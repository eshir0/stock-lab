"""Official ranking / investor-flow context: parsing, freshness, the refresh loop and the AI selection input."""
import pytest

from app.config import Config
from app.engine import Engine
from app.intel import FLOW_TTL, MAX_AGE, OUTSIDE, RANK_KINDS, RANK_RETRY, RANK_TTL, MarketIntel, parse_investor_trading, parse_rankings
from app.providers import DemoProvider, ProviderError, RateLimited
from app.store import Store


def ranking_row(rank, symbol, change='0.0125'):
    return {'rank': rank, 'symbol': symbol, 'currency': 'KRW', 'tradingVolume': '10', 'tradingAmount': '20',
            'price': {'lastPrice': '100', 'basePrice': '99', 'changeRate': change}}


def flow_record(date, foreigner, institution, individual=None):
    part = lambda n: {'buyVolume': '0', 'sellVolume': '0', 'netBuyVolume': str(n)}
    return {'date': date, 'updatedAt': '2026-09-29T10:00:00+09:00', 'foreigner': part(foreigner),
            'institution': part(institution), 'individual': part(individual) if individual is not None else None}


# ---- parsing ---------------------------------------------------------------------------------------------
def test_rankings_are_parsed_defensively():
    parsed = parse_rankings({'rankedAt': 'x', 'rankings': [ranking_row(1, '005930'), ranking_row(2, '000660', None),
                                                              {'rank': 'x', 'symbol': 'BAD'}, {'symbol': 5}, 'junk',
                                                              ranking_row(0, 'ZERO')]})
    assert parsed == {'items': {'005930': {'rank': 1, 'change_pct': 1.25}, '000660': {'rank': 2, 'change_pct': None}}}
    assert parse_rankings({'rankings': []}) == {'items': {}}
    for junk in (None, [], {'rankings': 'nope'}, 'text'):
        assert parse_rankings(junk) is None


def test_investor_trading_uses_the_latest_day_and_sums_recent_days():
    result = {'records': [flow_record('2026-09-29', 1000, -500), flow_record('2026-09-28', -200, 300, 50),
                          flow_record('2026-09-27', 100, 100, 10)]}
    flows = parse_investor_trading(result, today='2026-09-29')
    assert flows['date'] == '2026-09-29' and flows['provisional'] is True and flows['days'] == 3
    assert (flows['foreigner_net_shares'], flows['institution_net_shares'], flows['individual_net_shares']) == (1000, -500, None)
    assert (flows['foreigner_5d_net_shares'], flows['institution_5d_net_shares']) == (900, -100)
    assert parse_investor_trading(result, today='2026-09-30')['provisional'] is False
    for junk in (None, {'records': []}, {'records': ['x']}, {}):
        assert parse_investor_trading(junk) is None


# ---- cache ---------------------------------------------------------------------------------------------------
class Clock:
    now = 1_000_000.0

    def __call__(self):
        return self.now


def test_features_report_rank_or_outside_and_expire_when_stale():
    clock = Clock()
    intel = MarketIntel(clock)
    assert intel.features('005930', 'KR') == {'rankings': None, 'investor_flows': None}
    intel.store_rankings('KR', {'volume': {'005930': {'rank': 3, 'change_pct': 1.5}},
                                'gainers': {'000660': {'rank': 9, 'change_pct': 7.0}}})
    clock.now += 125
    first = intel.features('005930', 'KR')['rankings']
    assert first == {'as_of_minutes_ago': 2, 'volume_rank': 3, 'gainers_rank': OUTSIDE, 'change_vs_prev_close_pct': 1.5}
    second = intel.features('000660', 'KR')['rankings']
    assert second['volume_rank'] == OUTSIDE and second['gainers_rank'] == 9 and 'change_vs_prev_close_pct' not in second
    assert intel.features('AAPL', 'US')['rankings'] is None            # a different market has no data
    clock.now += MAX_AGE
    assert intel.features('005930', 'KR')['rankings'] is None          # stale rankings are never shown as current


def test_flows_carry_their_age_and_a_failed_fetch_shows_nothing():
    clock = Clock()
    intel = MarketIntel(clock)
    intel.store_flow('005930', {'date': '2026-09-29', 'foreigner_net_shares': 5})
    clock.now += 600
    assert intel.features('005930', 'KR')['investor_flows'] == {'date': '2026-09-29', 'foreigner_net_shares': 5, 'as_of_minutes_ago': 10}
    intel.store_flow('000660', None)
    assert intel.features('000660', 'KR')['investor_flows'] is None and intel.flow_age('000660') == 0


# ---- refresh loop ----------------------------------------------------------------------------------------------
class IntelProvider(DemoProvider):
    def __init__(self):
        self.rank_calls, self.flow_calls, self.rank_fail, self.flow_fail, self.rank_busy = [], [], set(), False, set()

    def rankings(self, country, kind, duration='realtime', count=100):
        self.rank_calls.append((country, kind, duration))
        if (country, kind) in self.rank_busy:
            raise RateLimited('busy')
        if kind in self.rank_fail:
            raise ProviderError('down')
        return {'rankedAt': 'now', 'rankings': [ranking_row(1, '005930'), ranking_row(2, 'AAPL')]}

    def investor_trading(self, symbol, count=5):
        self.flow_calls.append(symbol)
        if self.flow_fail:
            raise ProviderError('down')
        return {'records': [flow_record('2026-09-29', 10, 20)]}


@pytest.fixture
def engine(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'intel.db'), mode='demo', password='test-password-123456',
                    session_secret='test-secret-123456789012345678901234', toss_id='', toss_secret='', gemini_key='')
    store = Store(config.database_url, config.mode)
    engine = Engine(config, store, IntelProvider())
    engine.boot()
    engine.new_experiment(1000000, 1000, 'intel', strategy_mode='intraday')
    engine.refresh()
    engine.intel.clock = Clock()
    engine.intel_pause = 0
    yield engine
    store.release()


def test_first_refresh_fetches_every_market_and_korean_flows_then_waits(engine):
    engine.refresh_intel()
    provider = engine.provider
    assert len(provider.rank_calls) == 2*len(RANK_KINDS) and {c[0] for c in provider.rank_calls} == {'KR', 'US'}
    assert ('KR', 'TOP_GAINERS', '1d') in provider.rank_calls and ('KR', 'MARKET_TRADING_VOLUME', 'realtime') in provider.rank_calls
    assert sorted(provider.flow_calls) == ['000660', '005930', '122630']
    engine.refresh_intel()
    assert len(provider.rank_calls) == 2*len(RANK_KINDS) and len(provider.flow_calls) == 3


def test_rankings_refresh_every_minute_and_flows_every_five_while_the_market_is_open(engine):
    engine.refresh_intel()
    engine.intel.clock.now += RANK_TTL+1
    engine.refresh_intel()
    assert len(engine.provider.rank_calls) == 4*len(RANK_KINDS) and len(engine.provider.flow_calls) == 3
    engine.intel.clock.now += FLOW_TTL
    engine.refresh_intel()
    assert len(engine.provider.flow_calls) == 6


def test_failures_only_remove_context_and_never_raise(engine):
    engine.provider.rank_fail = {'MARKET_TRADING_VOLUME', 'TOP_LOSERS'}
    engine.provider.flow_fail = True
    engine.refresh_intel()
    kr = engine.intel.features('005930', 'KR')
    assert set(kr['rankings']) == {'as_of_minutes_ago', 'amount_rank', 'gainers_rank', 'change_vs_prev_close_pct'}
    assert kr['investor_flows'] is None
    engine.provider.rank_fail = {kind for _, kind, _ in RANK_KINDS}
    engine.intel.clock.now += RANK_TTL+1
    engine.refresh_intel()                                             # every ranking fails: nothing stored, nothing raised
    assert engine.intel.features('005930', 'KR')['rankings'] is not None   # the older snapshot is still within its age limit


def test_a_provider_without_rankings_is_ignored(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'plain.db'), mode='demo', password='test-password-123456',
                    session_secret='test-secret-123456789012345678901234', toss_id='', toss_secret='', gemini_key='')
    store = Store(config.database_url, config.mode)
    try:
        Engine(config, store, DemoProvider()).refresh_intel()
    finally:
        store.release()


# ---- what the AI receives -----------------------------------------------------------------------------------------
def test_selection_sees_rankings_and_flows_and_later_roles_get_market_intel(engine):
    engine.refresh_intel()
    engine.intel.clock.now = __import__('time').time()          # features() compares with the real wall clock in the cycle
    engine.intel.store_rankings('KR', {'volume': {'005930': {'rank': 1, 'change_pct': 1.2}}})
    engine.intel.store_rankings('US', {'volume': {'AAPL': {'rank': 4, 'change_pct': 0.4}}})
    engine.intel.store_flow('005930', {'date': '2026-09-29', 'foreigner_net_shares': 77})
    seen = {'selector': [], 'roles': {}}
    original = engine.agents.run

    def run(role, context, generation):
        if role == 'selector':
            seen['selector'] = context['candidates']
        else:
            seen['roles'][role] = context.get('market_intel')
        return original(role, context, generation)
    engine.agents.run = run
    engine.start()
    engine.cycle()
    run_record = engine.store.read()['runs'][-1]
    first = {c['symbol']: c for c in seen['selector']}
    assert set(first) <= {'005930', '000660', '122630'} or set(first) <= {'AAPL', 'MSFT', 'TQQQ', 'SQQQ'}
    sample = next(iter(first.values()))
    assert 'rankings' in sample and 'investor_flows' in sample
    assert run_record['reports'][0]['role'] == 'selector' and run_record['reports'][0]['inputs'] == seen['selector']
    assert set(seen['roles']) >= {'planner', 'director'} and all(v is not None for v in seen['roles'].values())


def test_status_summarises_freshness_for_the_dashboard():
    clock = Clock()
    intel = MarketIntel(clock)
    assert intel.status() == {'rankings': {}, 'flows': {'count': 0, 'tried': 0}, 'errors': {}}
    intel.store_rankings('KR', {'volume': {}, 'gainers': {}})
    intel.store_flow('005930', {'date': 'd'})
    intel.store_flow('000660', None)
    clock.now += 130
    assert intel.status() == {'rankings': {'KR': {'age': 130, 'kinds': ['gainers', 'volume']}},
                              'flows': {'count': 1, 'tried': 2}, 'errors': {}}
    intel.note_error('US', 'gainers', 'HTTP 400 unsupported-ranking-duration')
    assert intel.status()['errors'] == {'US:gainers': 'HTTP 400 unsupported-ranking-duration'}
    intel.note_error('US', 'gainers', '')
    assert intel.status()['errors'] == {}


def test_refresh_records_why_a_ranking_failed_and_clears_it_on_success(engine):
    engine.provider.rank_fail = {'TOP_GAINERS'}
    engine.refresh_intel()
    assert set(engine.intel.status()['errors']) == {'KR:gainers', 'US:gainers'}
    engine.provider.rank_fail = set()
    engine.intel.clock.now += RANK_TTL+1
    engine.refresh_intel()
    assert engine.intel.status()['errors'] == {}


def test_a_failed_list_is_retried_alone_after_the_retry_delay(engine):
    engine.provider.rank_fail = {'TOP_LOSERS'}
    engine.refresh_intel()
    assert len(engine.provider.rank_calls) == 8 and set(engine.intel.status()['errors']) == {'KR:losers', 'US:losers'}
    engine.provider.rank_fail = set()
    engine.intel.clock.now += RANK_RETRY-5
    engine.refresh_intel()
    assert len(engine.provider.rank_calls) == 8                        # too soon
    engine.intel.clock.now += 6
    engine.refresh_intel()
    assert engine.provider.rank_calls[8:] == [('KR', 'TOP_LOSERS', '1d'), ('US', 'TOP_LOSERS', '1d')]
    assert engine.intel.status()['errors'] == {} and engine.intel.kinds('KR') == {kind for kind, _, _ in RANK_KINDS}
    engine.intel.clock.now += 3*RANK_RETRY
    engine.refresh_intel()
    assert len(engine.provider.rank_calls) == 10                       # complete snapshots are not refetched early


def test_a_busy_ranking_group_ends_the_round_instead_of_being_hammered(engine):
    engine.provider.rank_busy = {('KR', 'MARKET_TRADING_AMOUNT')}
    engine.refresh_intel()
    kr_calls = [c for c in engine.provider.rank_calls if c[0] == 'KR']
    assert [c[1] for c in kr_calls] == ['MARKET_TRADING_VOLUME', 'MARKET_TRADING_AMOUNT']   # stopped at the busy call
    assert len([c for c in engine.provider.rank_calls if c[0] == 'US']) == len(RANK_KINDS)  # the other market is unaffected
    assert engine.intel.kinds('KR') == {'volume'} and 'KR:amount' in engine.intel.status()['errors']
    engine.provider.rank_busy = set()
    engine.intel.clock.now += RANK_RETRY+1
    engine.refresh_intel()
    assert engine.intel.kinds('KR') == {kind for kind, _, _ in RANK_KINDS}     # the missing lists were merged in
