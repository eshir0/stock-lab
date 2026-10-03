"""A pick the account cannot buy even one share of only wastes a slot (and an analysis). The focus list checks each name's
price against the per-name limit of the experiment's own virtual account, and refits a list built before that check.
Throw-away ledger and synthetic candles; no market data or AI service is contacted."""
import pytest

from app import universe as u
from app.config import Config
from app.engine import Engine
from app.instruments import CATALOGUE
from app.store import Store
from test_focus_engine import FocusProvider, PASSWORD, SECRET, build, open_now, series
from test_universe import NOW, candles, item, trend

COSTS = 1.0005*1.0015*1.03            # slippage 5 bp, fee 15 bp, and the 3% cushion (see Engine.position_cap)


def account(tmp_path, krw=1_000_000, usd=1000, ratio=30, fractional=False, **settings):
    # US names are affordable by definition when fractional shares are on; these tests are about whole shares.
    config = Config(database_url='sqlite:///'+str(tmp_path/'afford.db'), mode='demo', password=PASSWORD, session_secret=SECRET,
                    toss_id='', toss_secret='', gemini_key='', fee_kr=15, fee_us=15, sell_tax_kr=0, slippage_bps=5, fractional_us=fractional)
    store = Store(config.database_url, config.mode)
    engine = Engine(config, store, FocusProvider())
    engine.focus_pause = engine.focus_retry_pause = 0
    engine.boot()
    engine.new_experiment(krw, usd, 'afford', ratio, 'intraday', {'horizon': 'month', **settings})
    open_now(engine)
    return engine, store


# ---- the arithmetic -----------------------------------------------------------------------------------------------

def test_the_cap_is_the_smaller_of_the_per_order_and_per_name_limits_less_costs(tmp_path):
    engine, store = account(tmp_path)
    state = engine.store.read()
    assert engine.position_cap(state, 'USD') == pytest.approx(1000*.30/COSTS, rel=1e-6)
    assert engine.position_cap(state, 'KRW') == pytest.approx(1_000_000*.30/COSTS, rel=1e-6)
    store.release()
    small, store = account(tmp_path, ratio=10)
    assert small.position_cap(small.store.read(), 'USD') == pytest.approx(1000*.10/COSTS, rel=1e-6)      # the per-order limit is tighter
    store.release()


def test_the_reason_names_the_price_and_the_limit_and_nothing_is_checked_without_a_budget():
    m = {'last': 450.0}
    assert u.unaffordable(m, item('AAPL'), None) == [] and u.unaffordable(m, item('AAPL'), {}) == []
    assert u.unaffordable(m, item('AAPL'), {'KRW': 5}) == []                       # another currency's limit says nothing about this one
    assert u.unaffordable({'last': 290.0}, item('AAPL'), {'USD': 290.6}) == []
    reason = u.unaffordable(m, item('AAPL'), {'USD': 290.62})
    assert len(reason) == 1 and '450.00' in reason[0] and '290.62' in reason[0] and '살 수 없음' in reason[0]
    assert '원금이 없어' in u.unaffordable(m, item('AAPL'), {'USD': 0})[0]
    assert '300,000' in u.unaffordable({'last': 1_800_000.0}, item('005930'), {'KRW': 300_000.0})[0]


def test_the_ranking_leaves_out_names_above_the_cap_and_keeps_the_screens_own_reasons():
    pool = [item('AAPL'), item('MSFT')]
    cheap, dear = u.daily_metrics(candles(trend(base=100.0)), now=NOW), u.daily_metrics(candles(trend(base=400.0)), now=NOW)
    passed, excluded = u.rank_pool(pool, {'AAPL': cheap, 'MSFT': dear}, budget={'USD': 300.0})
    assert [p['symbol'] for p in passed] == ['AAPL'] and excluded[0]['symbol'] == 'MSFT' and '살 수 없음' in excluded[0]['reasons'][0]
    everyone, _ = u.rank_pool(pool, {'AAPL': cheap, 'MSFT': dear})                  # without a budget nothing changes
    assert {p['symbol'] for p in everyone} == {'AAPL', 'MSFT'}


# ---- the list ---------------------------------------------------------------------------------------------------------------

def strong(engine, **prices):
    """Every name trends up gently; the named ones start from the given price and trend harder, so they would top the list."""
    engine.provider.series = {c['symbol']: series(c['demo_base'], daily=.002) for c in CATALOGUE}
    for symbol, base in prices.items():
        engine.provider.series[symbol] = series(base, daily=.006)


def test_a_small_account_only_gets_names_it_can_buy(tmp_path):
    engine, store = account(tmp_path)
    strong(engine, META=700, TSLA=400, NVDA=180)                  # NVDA fits under about $290, META and TSLA do not
    entry = build(engine)['focus']['US']
    symbols = [p['symbol'] for p in entry['picks']]
    assert 'NVDA' in symbols and not {'META', 'TSLA'} & set(symbols)
    assert all(p['last'] <= entry['afford']['USD'] for p in entry['picks']) and entry['afford']['USD'] == pytest.approx(290.6, abs=.1)
    why = {e['symbol']: ' '.join(e['reasons']) for e in entry['excluded']}
    assert '살 수 없음' in why['META'] and '살 수 없음' in why['TSLA']
    store.release()


def test_with_fractional_us_shares_no_us_name_is_too_expensive(tmp_path):
    engine, store = account(tmp_path, fractional=True)
    strong(engine, META=700, TSLA=400, NVDA=180)
    entry = build(engine)['focus']['US']
    assert {'META', 'TSLA', 'NVDA'} <= {p['symbol'] for p in entry['picks']} and entry['afford'] == {'KRW': entry['afford']['KRW']}
    assert not any('살 수 없음' in ' '.join(e['reasons']) for e in entry['excluded'])
    store.release()


def test_a_bigger_account_can_take_the_expensive_names(tmp_path):
    engine, store = account(tmp_path, usd=10_000)
    strong(engine, META=700, TSLA=400)
    entry = build(engine)['focus']['US']
    assert {'META', 'TSLA'} <= {p['symbol'] for p in entry['picks']}
    assert not any('살 수 없음' in ' '.join(e['reasons']) for e in entry['excluded'])
    store.release()


def test_a_currency_without_any_virtual_money_gets_an_empty_list(tmp_path):
    engine, store = account(tmp_path, usd=0)
    strong(engine)
    entry = build(engine)['focus']['US']
    assert entry['picks'] == [] and entry['excluded'] and all('원금이 없어' in ' '.join(e['reasons']) for e in entry['excluded'])
    store.release()


# ---- a list built before the check existed --------------------------------------------------------------------------------------

def test_an_older_list_is_refitted_once_keeping_what_fits(tmp_path):
    engine, store = account(tmp_path, usd=10_000)
    strong(engine, META=700, NVDA=180)
    entry = build(engine)['focus']['US']
    assert 'META' in [p['symbol'] for p in entry['picks']] and 'NVDA' in [p['symbol'] for p in entry['picks']]
    with engine.store.edit() as s:                                # the account is small, and this list predates the check
        s['cash']['USD'] = s['initial']['USD'] = 1000.0
        s['focus']['US'].pop('afford')
        s['focus']['US']['picks'][[p['symbol'] for p in entry['picks']].index('NVDA')].update(source='ai', ai={'theme': 't', 'catalyst': 'c', 'priced_in_risk': 'low', 'reason': 'r'})
    engine.refresh_focus()
    state = engine.store.read()
    fixed = state['focus']['US']
    symbols = [p['symbol'] for p in fixed['picks']]
    assert 'META' not in symbols and 'NVDA' in symbols and len(symbols) == 3
    assert next(p for p in fixed['picks'] if p['symbol'] == 'NVDA')['source'] == 'ai'          # the AI's own affordable pick stays
    assert fixed['afford']['USD'] == pytest.approx(290.6, abs=.1)
    assert 'META' not in [p['symbol'] for p in fixed['ranked']] and any(e['symbol'] == 'META' and '살 수 없음' in e['reasons'][0] for e in fixed['excluded'])
    record = [r for r in state['focus_history'] if r['market'] == 'US'][-1]
    assert record['picks'] == symbols
    events = [e['message'] for e in state['events'] if '1주도 살 수 없는 종목' in e['message']]
    assert len(events) == 1 and 'Meta Platforms' in events[0]
    engine.refresh_focus()                                        # once per list: nothing more happens
    assert engine.store.read()['focus']['US']['picks'] == fixed['picks']
    assert len([e for e in engine.store.read()['events'] if '1주도 살 수 없는 종목' in e['message']]) == 1
    store.release()


def test_a_fallback_or_missing_list_is_left_alone(tmp_path):
    engine, store = account(tmp_path)
    engine.refit_focus('US', 'any')
    with engine.store.edit() as s:
        s['focus']['US'] = {'market': 'US', 'session_date': 'x', 'status': 'fallback', 'picks': [], 'excluded': [], 'ranked': []}
    engine.refit_focus('US', 'x')
    assert 'afford' not in engine.store.read()['focus']['US']
    store.release()


# ---- risk-based affordability (2026-10-03): a name whose single share risks more than one trade may lose is left out -------

from app import universe as _u


def test_one_share_must_fit_the_risk_per_trade():
    item = {'currency': 'KRW'}
    m = {'last': 1_841_000, 'atr_pct': 6.0}                                  # one share at a 6% stop risks 110,460
    assert _u.unaffordable(m, item, {'KRW': 2_000_000}) == []                # the old check only looked at the price ...
    assert _u.unaffordable(m, item, {'KRW': 2_000_000}, {'KRW': 15_000})     # ... a 3M account at 0.5% may lose 15,000
    assert _u.unaffordable(m, item, {'KRW': 2_000_000}, {'KRW': 120_000}) == []    # a bigger account takes it again
    cheap = {'last': 81_100, 'atr_pct': 1.0}                                 # the 2% floor applies: 1,622 per share
    assert _u.unaffordable(cheap, item, {'KRW': 900_000}, {'KRW': 15_000}) == []
    assert _u.unaffordable(m, {'currency': 'USD'}, None, {'KRW': 1}) == []   # other currencies and fractional shares: no check
