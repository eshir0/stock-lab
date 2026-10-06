"""The daily focus list inside the engine: timing, fallbacks, buy limits, rotation of held names, outcome scoring.

Everything runs on a throw-away SQLite ledger with a synthetic provider; no market data or AI service is contacted.
"""
import time

import pytest
from fastapi.testclient import TestClient

from app import universe
from app.config import Config
from app.engine import Engine
from app.instruments import CATALOGUE, INSTRUMENTS, SYMBOLS
from app.main import create_app
from app.providers import DemoProvider, ProviderError, RateLimited
from app.store import Store

DAY = 86400
PASSWORD, SECRET = 'test-password-123456', 'test-secret-123456789012345678901234'


def series(base, n=40, daily=.004, volume=1e9, spread=.01, end=None):
    end = int(time.time()//DAY)*DAY if end is None else end
    return [{'time': end-(n-i)*DAY, 'open': base*(1+daily)**i, 'high': base*(1+daily)**i*(1+spread),
             'low': base*(1+daily)**i*(1-spread), 'close': base*(1+daily)**i, 'volume': volume,
             'currency': 'USD', 'interval': '1d', 'completed': True} for i in range(n)]


class FocusProvider(DemoProvider):
    """Synthetic quotes with controllable sessions; daily candles come from `self.series` (a healthy uptrend by default)."""

    def __init__(self):
        self.series = {}
        self.calls = []
        self.fail = set()
        self.limited_from = None        # raise RateLimited once this many candle reads have happened
        self.limit_once = set()         # symbols that answer 429 exactly once
        self.sessions = {}

    def quote(self, symbol):
        q = super().quote(symbol)
        window = self.sessions.get(SYMBOLS[symbol]['market'])
        if window:
            q.update(session_start=window[0], session_end=window[1])
        return q

    def candles(self, symbol, interval='1d', count=None):
        if interval != '1d':
            return super().candles(symbol, interval)
        if self.limited_from is not None and len(self.calls) >= self.limited_from:
            raise RateLimited('chart group busy')
        if symbol in self.limit_once:
            self.limit_once.discard(symbol)
            raise RateLimited('chart group busy')
        self.calls.append(symbol)
        if symbol in self.fail:
            raise ProviderError('candles unavailable')
        if symbol in self.series:
            return self.series[symbol]
        index = [c['symbol'] for c in CATALOGUE].index(symbol)
        return series(SYMBOLS[symbol]['demo_base'], daily=.0015+(index % 7)*.0006)


def make(tmp_path, mode='demo', settings=None):
    config = Config(database_url='sqlite:///'+str(tmp_path/'focus.db'), mode=mode, password=PASSWORD, session_secret=SECRET,
                    toss_id='', toss_secret='', gemini_key='', fee_kr=15, fee_us=15, sell_tax_kr=0, slippage_bps=5)
    store = Store(config.database_url, config.mode)
    engine = Engine(config, store, FocusProvider())
    engine.focus_pause = engine.focus_retry_pause = 0
    engine.boot()
    # These tests are about the rising-trend screen (a month plan); day trading has its own file, test_day_focus.py.
    # A large virtual account, so none of these tests is about affordability (test_affordability.py is).
    engine.new_experiment(10_000_000, 10_000, 'focus test', strategy_mode='intraday', strategy_settings={'horizon': 'month', **(settings or {})})
    return engine, store


@pytest.fixture
def engine(tmp_path):
    engine, store = make(tmp_path)
    open_now(engine)
    yield engine
    store.release()


def open_now(engine, kr=(-1800, 6*3600), us=(-1800, 6*3600)):
    now = time.time()
    engine.provider.sessions = {'KR': (now+kr[0], now+kr[1]), 'US': (now+us[0], now+us[1])}


def build(engine):
    engine.refresh()
    engine.refresh_focus()
    return engine.store.read()


def picks(state, market):
    return [p['symbol'] for p in state['focus'][market]['picks']]


# ---- when the list is built -------------------------------------------------------------------------------------------

def test_the_list_is_built_only_inside_the_lead_window_before_the_open(engine):
    now = time.time()
    engine.provider.sessions = {'KR': (now+3*3600, now+9*3600), 'US': (now+3600, now+7*3600)}   # opens in 3 h / in 1 h
    state = build(engine)
    assert 'KR' not in state['focus'] and 'US' in state['focus']
    engine.provider.sessions = {'KR': (now-10*3600, now-3600), 'US': (now+3600, now+7*3600)}    # KR already closed
    engine.focus_attempts.clear()
    assert 'KR' not in build(engine)['focus']


def test_a_market_without_a_regular_session_today_is_skipped(engine):
    now = time.time()
    engine.provider.sessions = {'KR': (0, 0), 'US': (now-600, now+6*3600)}      # holiday: the calendar gives no session
    assert list(build(engine)['focus']) == ['US']


def test_one_list_per_market_per_session_and_no_refetching(engine):
    build(engine)
    calls = len(engine.provider.calls)
    for _ in range(3):
        engine.refresh_focus()
    assert len(engine.provider.calls) == calls and len(engine.store.read()['focus_history']) == 2


def test_a_new_session_builds_a_new_list(engine):
    build(engine)
    with engine.store.edit() as s:
        for entry in s['focus'].values():
            entry['session_date'] = '1999-12-31'
    before = len(engine.provider.calls)
    engine.refresh_focus()
    assert len(engine.provider.calls) > before


def test_the_fixed_universe_mode_never_builds_a_list(tmp_path):
    engine, store = make(tmp_path, settings={'universe_mode': 'fixed'})
    open_now(engine)
    assert build(engine)['focus'] == {} and engine.provider.calls == []
    assert [i['symbol'] for i in engine.active_instruments(engine.store.read())] == [i['symbol'] for i in INSTRUMENTS]
    store.release()


# ---- what the list contains ---------------------------------------------------------------------------------------------

def test_the_list_is_a_small_subset_of_the_closed_catalogue_per_market(engine):
    state = build(engine)
    for market in ('KR', 'US'):
        symbols = picks(state, market)
        assert 1 <= len(symbols) <= engine.c.focus_per_market
        assert all(SYMBOLS[s]['market'] == market for s in symbols) and len(set(symbols)) == len(symbols)
    assert state['focus']['US']['candidates'] == 19 and state['focus']['KR']['candidates'] == 17


def test_leveraged_etfs_are_never_picked_when_the_experiment_forbids_them(tmp_path):
    engine, store = make(tmp_path, settings={'include_leveraged_etfs': False})
    open_now(engine)
    engine.provider.series = {s: series(SYMBOLS[s]['demo_base'], daily=.006) for s in ('TQQQ', 'SQQQ', '122630', '252670')}
    state = build(engine)
    everything = [p['symbol'] for m in ('KR', 'US') for p in state['focus'][m]['picks']]
    assert not set(everything) & {'TQQQ', 'SQQQ', '122630', '252670'}
    assert state['focus']['US']['candidates'] == 17 and state['focus']['KR']['candidates'] == 15
    store.release()


def test_downtrend_stretched_and_broken_data_names_are_excluded_with_reasons(engine):
    engine.provider.series = {'NVDA': series(180, daily=-.004), 'AMD': series(200, n=40, daily=.022),
                              'TSLA': series(400, n=10), 'META': series(700, daily=.006)}
    engine.provider.fail = {'AMZN'}
    entry = build(engine)['focus']['US']
    why = {e['symbol']: ' '.join(e['reasons']) for e in entry['excluded']}
    assert '20일선 아래' in why['NVDA'] and '추격 금지' in why['AMD'] and why['TSLA'] == '일봉 데이터 부족' and why['AMZN'] == '일봉 데이터 부족'
    assert not {'NVDA', 'AMD', 'TSLA', 'AMZN'} & set(picks({'focus': {'US': entry}}, 'US'))
    assert 'META' in [p['symbol'] for p in entry['picks']]           # the strongest healthy trend still makes it


def test_the_traded_value_floor_applies_in_toss_mode(tmp_path):
    engine, store = make(tmp_path, mode='toss')
    open_now(engine)
    engine.provider.series = {'NVDA': series(180, volume=1000, daily=.006)}
    entry = build(engine)['focus']['US']
    assert any(e['symbol'] == 'NVDA' and '거래대금' in ' '.join(e['reasons']) for e in entry['excluded'])
    store.release()


def test_reference_prices_and_working_data_stay_server_side(engine):
    build(engine)
    public = engine.public_state()
    assert all('ranked' not in e and 'metrics' not in e for e in public['focus'].values())
    assert all('ref' not in r for r in public['focus_history'])
    assert public['focus_config'] == {'mode': 'daily_focus', 'per_market': 3, 'ai': True, 'profile': 'trend'}


# ---- the AI read: optional, checked, never trusted with symbols ----------------------------------------------------------

def read(picks=(), avoid=(), grounded=True):
    return {'market_view': '시장 분위기', 'themes': ['반도체'], 'grounded': grounded, 'engine': 'test',
            'picks': [{'symbol': s, 'theme': 't', 'catalyst': 'c', 'priced_in_risk': r, 'reason': 'x'} for s, r in picks],
            'avoid': [{'symbol': s, 'reason': 'bad'} for s in avoid], 'risks': [], 'evidence': [{'claim': 'c', 'source_url': 'https://n.example/a'}]}


def test_without_a_running_analysis_the_list_is_data_only_and_gets_the_brief_once_started(engine):
    state = build(engine)
    assert state['focus']['US']['status'] == 'quant_only' and state['focus']['US']['ai']['status'] == 'none'
    engine.start()
    calls = len(engine.provider.calls)
    engine.refresh_focus()
    upgraded = engine.store.read()['focus']['US']
    assert upgraded['status'] == 'ok' and upgraded['source'] == 'ai+data' and upgraded['ai']['status'] == 'ok'
    assert len(engine.provider.calls) == calls                       # the upgrade reuses the measurements
    assert len(engine.store.read()['focus_history']) == 2           # and does not add a second day record


def test_ai_picks_lead_and_the_data_ranking_fills_the_rest(engine):
    engine.start()
    state = build(engine)
    entry = state['focus']['US']
    assert entry['status'] == 'ok'
    assert [p['source'] for p in entry['picks']][:1] == ['ai'] and entry['ai']['market_view']


def test_an_invented_symbol_a_priced_in_pick_and_an_avoid_are_all_handled_on_the_server(engine):
    engine.start()
    offered_us = []

    def fake(role, context, generation):
        offered = [c['symbol'] for c in context['candidates']]
        if context['market'] == 'US':
            offered_us[:] = offered
        return read(picks=[('ZZZZ', 'low'), (offered[1], 'high'), (offered[2], 'low')], avoid=[offered[0]])
    engine.agents.run = fake
    symbols = [p['symbol'] for p in build(engine)['focus']['US']['picks']]
    assert 'ZZZZ' not in symbols                                     # the AI cannot add a symbol
    assert offered_us[0] not in symbols and offered_us[1] not in symbols   # avoided / priced-in names stay out, even as filler
    assert symbols[0] == offered_us[2] and all(SYMBOLS[s]['market'] == 'US' for s in symbols)


def test_an_ai_failure_falls_back_to_the_data_ranking_and_says_so(engine):
    engine.start()

    def boom(role, context, generation):
        raise ProviderError('[Claude 사용량 소진] 대기 중')
    engine.agents.run = boom
    state = build(engine)
    entry = state['focus']['US']
    assert entry['status'] == 'quant_only' and entry['ai']['status'] == 'failed' and entry['picks']
    assert any('AI 뉴스 브리핑을 받지 못해' in e['message'] and e['level'] == 'warning' for e in state['events'])


def test_a_read_without_cited_sources_is_not_used(engine):
    engine.start()
    engine.agents.run = lambda role, context, generation: read(picks=[(context['candidates'][-1]['symbol'], 'low')], grounded=False)
    entry = build(engine)['focus']['US']
    assert entry['status'] == 'quant_only' and {p['source'] for p in entry['picks']} == {'data'}
    assert any('출처' in n for n in entry['notes'])
    ai_calls = []
    engine.agents.run = lambda *a: ai_calls.append(a)
    engine.refresh_focus()
    assert ai_calls == []                                            # an unsourced answer is not retried in a loop


def test_the_ai_can_be_switched_off_by_configuration(engine):
    engine.c.focus_ai = False
    engine.start()
    entry = build(engine)['focus']['US']
    assert entry['status'] == 'quant_only' and entry['ai']['status'] == 'skipped'


# ---- failures never block trading -----------------------------------------------------------------------------------------

def test_unreadable_market_data_keeps_the_fixed_lineup_and_gives_up_after_three_tries(engine):
    engine.provider.fail = {i['symbol'] for i in CATALOGUE}
    for attempt in range(3):
        engine.focus_attempts = {m: (0, failures, date) for m, (_, failures, date) in engine.focus_attempts.items()}
        state = build(engine)
    assert state['focus']['US']['status'] == 'fallback' and state['focus']['US']['picks'] == []
    assert sorted(i['symbol'] for i in engine.active_instruments(state)) == sorted(i['symbol'] for i in INSTRUMENTS)
    warnings = [e['message'] for e in state['events'] if '집중 종목 계산에 실패' in e['message']]
    assert len(warnings) == 6 and '기본 종목으로 진행' in warnings[-1]
    calls = len(engine.provider.calls)
    engine.refresh_focus()
    assert len(engine.provider.calls) == calls                       # gave up for this session, no retry storm


def test_a_failed_build_waits_before_the_next_attempt(engine):
    engine.provider.fail = {i['symbol'] for i in CATALOGUE}
    build(engine)
    calls = len(engine.provider.calls)
    engine.refresh_focus()
    assert len(engine.provider.calls) == calls


def test_a_busy_chart_group_leaves_the_rest_missing_but_still_publishes_when_enough_names_were_read(engine):
    engine.provider.limited_from = 10                    # the Korean pass reads 10 names, then the group answers 429
    focus = build(engine)['focus']
    assert focus['KR']['status'] == 'quant_only' and focus['KR']['measured'] == 10
    assert sum('일봉 데이터 부족' in ' '.join(e['reasons']) for e in focus['KR']['excluded']) == 7
    assert 'US' not in focus                             # nothing readable there: no list, fixed lineup stays


def test_a_short_429_is_retried_instead_of_dropping_the_name(engine):
    engine.provider.limit_once = {'AAPL', 'NVDA'}
    entry = build(engine)['focus']['US']
    missing = {e['symbol'] for e in entry['excluded'] if e['reasons'] == ['일봉 데이터 부족']}
    assert not missing and entry['measured'] == 19
    assert engine.provider.calls.count('AAPL') == 1                  # the retry succeeded once


def test_too_few_readable_names_publish_nothing(engine):
    engine.provider.fail = {i['symbol'] for i in CATALOGUE if i['market'] == 'US'} - {'AAPL', 'MSFT', 'NVDA'}
    state = build(engine)
    assert 'US' not in state['focus']
    assert [i['symbol'] for i in engine.active_instruments(state) if i['market'] == 'US'] == [
        i['symbol'] for i in INSTRUMENTS if i['market'] == 'US']


def test_a_stale_list_is_ignored(engine):
    state = build(engine)
    assert engine.focus_symbols(state, 'US') is not None
    assert engine.focus_symbols(state, 'US', now=time.time()+universe.MAX_AGE+60) is None


# ---- what the desk may buy ------------------------------------------------------------------------------------------------

def test_the_desk_quotes_and_analyses_only_todays_list_plus_anything_held(engine):
    state = build(engine)
    active = [i['symbol'] for i in engine.active_instruments(state)]
    assert active == picks(state, 'KR')+picks(state, 'US')
    assert set(active) < {c['symbol'] for c in CATALOGUE}


def test_a_market_without_a_list_uses_the_fixed_lineup_while_the_other_uses_its_list(engine):
    now = time.time()
    engine.provider.sessions = {'KR': (now+10*3600, now+16*3600), 'US': (now-600, now+6*3600)}
    state = build(engine)
    active = [i['symbol'] for i in engine.active_instruments(state)]
    assert [s for s in active if SYMBOLS[s]['market'] == 'KR'] == [i['symbol'] for i in INSTRUMENTS if i['market'] == 'KR']
    assert [s for s in active if SYMBOLS[s]['market'] == 'US'] == picks(state, 'US')


def test_new_buys_outside_the_list_are_refused_everywhere(engine):
    state = build(engine)
    outside = next(c['symbol'] for c in CATALOGUE if c['market'] == 'US' and c['symbol'] not in picks(state, 'US'))
    inside = picks(state, 'US')[0]
    engine.start()
    with engine.store.edit() as s:
        with pytest.raises(ProviderError, match='집중 종목'):
            engine.desk_buy_allowed(s, outside)
        engine.desk_buy_allowed(s, inside)                                                 # allowed
        with pytest.raises(ProviderError, match='집중 종목'):
            engine.fill(s, outside, 'BUY', 1, engine.provider.quote(outside), 'x')
    assert engine.request_cycle(inside) is None
    from app.engine import RuleError
    with pytest.raises(RuleError):
        engine.request_cycle(outside)


def test_the_analysis_cycle_only_ever_picks_a_symbol_from_the_list(engine):
    engine.start()
    state = build(engine)
    # the demo provider's candles satisfy the 20-minute warm-up, so a cycle runs end to end
    engine.cycle()
    run = engine.store.read()['runs'][-1]
    assert run['status'] == 'completed' and run['symbol'] in picks(state, SYMBOLS[run['symbol']]['market'])


# ---- names already held ------------------------------------------------------------------------------------------------------

def hold(engine, symbol, quantity=2):
    """Open a position the ordinary way BEFORE the list exists (the fixed lineup allows it)."""
    engine.refresh()
    quote = engine.provider.quote(symbol)
    with engine.store.edit() as s:
        engine.fill(s, symbol, 'BUY', quantity, quote, 'test-entry')
        engine.apply_desk_plan(s, symbol, {'side': 'BUY', 'summary': 'held before'},
                               {'stop_price': 1.0, 'take_profit_price': 1e9, 'expires_at': time.time()+5*3600, 'quantity': quantity})


def test_a_held_name_with_an_intact_trend_is_kept_and_never_added_to(engine):
    engine.set_execution('auto')
    hold(engine, '005930')
    engine.provider.series = {'005930': series(70000, daily=.0012)}      # healthy but the weakest of the Korean pool
    engine.start()
    state = build(engine)
    assert '005930' not in picks(state, 'KR')
    position = state['positions']['005930']
    assert position['rotation']['action'] == 'keep' and position['rotation']['trend_ok'] is True
    assert '005930' in [i['symbol'] for i in engine.active_instruments(state)]           # still quoted so exits keep working
    with engine.store.edit() as s:
        with pytest.raises(ProviderError, match='집중 종목'):
            engine.desk_buy_allowed(s, '005930')
    open_now(engine, kr=(-3000, 6*3600))
    engine.process_desk_exits()
    assert '005930' in engine.store.read()['positions']                                  # not sold: the normal rules govern it


def test_a_held_name_whose_trend_broke_is_sold_after_the_opening_minutes(engine):
    engine.set_execution('auto')
    hold(engine, '005930')
    engine.provider.series = {'005930': series(70000, daily=-.004)}
    engine.start()
    state = build(engine)
    assert state['positions']['005930']['rotation']['action'] == 'sell'
    assert any('오늘의 집중 종목에서 빠졌습니다' in e['message'] and '매도' in e['message'] for e in state['events'])
    open_now(engine, kr=(-300, 6*3600))                                                   # opened five minutes ago
    engine.process_desk_exits()
    assert '005930' in engine.store.read()['positions']                                  # opening noise: not yet
    open_now(engine, kr=(-1500, 6*3600))                                                  # 25 minutes after the open
    engine.process_desk_exits()
    after = engine.store.read()
    assert '005930' not in after['positions']
    assert after['trades'][-1]['side'] == 'SELL' and after['trades'][-1]['exit_reason'] == '종목 교체 청산'


def test_a_held_name_that_is_picked_again_loses_its_rotation_flag(engine):
    hold(engine, '005930')
    engine.provider.series = {'005930': series(70000, daily=.0012)}
    build(engine)
    with engine.store.edit() as s:
        assert 'rotation' in s['positions']['005930']
        s['focus']['KR']['session_date'] = '1999-12-31'
    engine.provider.series = {'005930': series(70000, daily=.0058)}                        # now the strongest trend in the pool
    engine.focus_attempts.clear()
    engine.refresh_focus()
    state = engine.store.read()
    assert '005930' in picks(state, 'KR') and 'rotation' not in state['positions']['005930']


# ---- measuring whether the picks were any good ---------------------------------------------------------------------------------

def test_earlier_days_are_scored_against_the_whole_pool_once_the_next_candle_exists(engine):
    build(engine)
    first = engine.store.read()['focus_history']
    us_picks = [p for p in first if p['market'] == 'US'][0]['picks']
    with engine.store.edit() as s:
        for entry in s['focus'].values():
            entry['session_date'] = '1999-12-31'
        for record in s['focus_history']:
            record['session_date'] = '1999-12-31'
    end = int(time.time()//DAY)*DAY
    for item in CATALOGUE:
        rows = engine.provider.candles(item['symbol'])
        gain = 1.03 if item['symbol'] in us_picks else 1.01
        engine.provider.series[item['symbol']] = rows+[{**rows[-1], 'time': end, 'close': rows[-1]['close']*gain}]
    engine.focus_attempts.clear()
    engine.refresh_focus()
    state = engine.store.read()
    resolved = [r for r in state['focus_history'] if r['market'] == 'US' and r['session_date'] == '1999-12-31'][0]['result']
    assert resolved['pick_pct'] == pytest.approx(3.0, abs=.01) and resolved['pool_pct'] < 3.0
    assert resolved['excess_pool_pct'] > 0 and resolved['n_picks'] == len(us_picks)
    public = engine.public_state()
    assert public['focus_eval']['US']['days'] == 1 and public['focus_eval']['US']['beat_pool_days'] == 1
    assert len(public['focus_history']) <= 10


def test_a_list_built_for_an_account_that_was_replaced_meanwhile_is_discarded(engine):
    stale_snapshot = engine.store.read()
    engine.new_experiment(1000000, 1000, 'second experiment', strategy_mode='intraday')
    engine.publish_focus(stale_snapshot, 'US', {'picks': [], 'ai': {}, 'status': 'ok', 'metrics': {}, 'session_date': 'x'}, set(), None, [], False, time.time())
    assert engine.store.read()['focus'] == {}


# ---- settings and the public surface ---------------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'api.db'), mode='demo', password=PASSWORD, session_secret=SECRET,
                    toss_id='', toss_secret='', gemini_key='')
    app = create_app(config, background=False, test=True)
    with TestClient(app) as c:
        c.headers.update({'X-Stocklab-Action': '1'})
        assert c.post('/api/login', json={'password': PASSWORD}).status_code == 200
        yield c


def experiment(**over):
    body = {'seed_krw': 1000000, 'seed_usd': 1000, 'name': 'x', 'max_order_pct': 30, 'strategy_mode': 'intraday',
            'include_leveraged_etfs': True, 'risk_per_trade_pct': .5, 'daily_loss_limit_pct': 2, 'max_holding_minutes': 120,
            'horizon': 'intraday', 'confirmation': '새 실험 시작'}
    body.update(over)
    return body


def test_the_experiment_form_accepts_both_universe_modes_and_defaults_to_the_daily_list(client):
    month = {'horizon': 'month', 'max_holding_minutes': 43200}                    # day trading was removed: month plans only
    assert client.post('/api/experiments', json=experiment(**month)).status_code == 200
    assert client.get('/api/state').json()['strategy_settings']['universe_mode'] == 'daily_focus'
    assert client.post('/api/experiments', json=experiment(universe_mode='fixed', scan='focus', **month)).status_code == 200
    state = client.get('/api/state').json()
    assert state['strategy_settings']['universe_mode'] == 'fixed' and state['focus_config']['mode'] == 'fixed'
    assert [i['symbol'] for i in state['instruments']] == [i['symbol'] for i in INSTRUMENTS]


def test_an_unknown_universe_mode_is_rejected_before_anything_changes(client):
    assert client.post('/api/experiments', json=experiment(universe_mode='everything')).status_code == 422
    assert client.get('/api/state').json()['strategy_settings'].get('universe_mode', 'daily_focus') == 'daily_focus'
