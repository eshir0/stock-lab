"""Pool scan (strategy_settings.scan == 'pool'): every pool name's completed daily bars are checked by the rules every 30
minutes; names a BUY rule flags (after the research filter, and only if one share fits the per-trade risk) join the poll and
may be bought. The focus list alone favours rising names, which the only Korean stock signal left (mean reversion) never
flags (2026-10-06)."""
import time

import pytest

from app import focus
from app.risk import normalize_settings
from test_month_desk import desk  # noqa: F401  (fixture)


def bars(price=50000.0, n=60):
    return [{'time': i, 'open': price, 'high': price*1.01, 'low': price*.99, 'close': price, 'volume': 1000} for i in range(n)]


def pool_mode(engine, scan='pool'):
    with engine.store.edit() as s:
        s['strategy_settings'].update(scan=scan, universe_mode='daily_focus', signal_filter='research')


@pytest.fixture
def flagged(desk, monkeypatch):
    """KB금융 (105560) flagged by mean reversion, 삼성전자 (005930) only by momentum (dropped for Korean stocks)."""
    def fake(rows):
        price = rows[-1]['close']
        if price == 61000.0:
            return {'golden_cross': None, 'momentum': 'HOLD', 'mean_reversion': 'BUY', 'breakout': None}
        if price == 62000.0:
            return {'golden_cross': None, 'momentum': 'BUY', 'mean_reversion': 'HOLD', 'breakout': None}
        return {'golden_cross': None, 'momentum': 'HOLD', 'mean_reversion': 'HOLD', 'breakout': None}
    prices = {'105560': 61000.0, '005930': 62000.0}
    monkeypatch.setattr(focus, 'rule_signals', fake)
    monkeypatch.setattr(desk, 'daily_bars', lambda symbol, now=None: bars(prices.get(symbol, 50000.0)))
    return desk


def test_a_flagged_pool_name_joins_the_watch_list_and_may_be_bought(flagged):
    pool_mode(flagged)
    flagged.scan_pool()
    state = flagged.store.read()
    assert '105560' in state['signal_names']['KR'] and '005930' not in state['signal_names']['KR']   # momentum is filtered
    assert '105560' in flagged.buyable_symbols(state) and flagged.focus_allows(state, '105560')
    assert '105560' in [i['symbol'] for i in flagged.active_instruments(state)]
    assert any('후보 전체 확인' in e['message'] for e in state['events'])


def test_the_scan_is_throttled_and_only_counts_while_fresh(flagged, monkeypatch):
    pool_mode(flagged)
    flagged.scan_pool()
    calls = []
    monkeypatch.setattr(flagged, 'daily_bars', lambda symbol, now=None: calls.append(symbol) or bars())
    flagged.scan_pool()
    assert calls == []                                                        # within 30 minutes
    state = flagged.store.read()
    assert flagged.signal_symbols(state, 'KR', time.time()+4*3600) == []      # an old scan adds nothing


def test_a_share_that_risks_more_than_one_trade_may_lose_is_left_out(flagged):
    pool_mode(flagged)
    with flagged.store.edit() as s:
        s['strategy_settings']['risk_per_trade_pct'] = .1                     # 1M KRW x 0.1% = 1,000 KRW per trade
    flagged.scan_pool()
    assert '105560' not in flagged.store.read()['signal_names']['KR']


def test_focus_mode_and_saved_experiments_do_not_scan(flagged):
    pool_mode(flagged, 'focus')
    flagged.scan_pool()
    state = flagged.store.read()
    assert not state.get('signal_names') and flagged.signal_symbols(state, 'KR') == []
    assert normalize_settings({})['scan'] == 'focus'


def test_the_form_offers_the_pool_scan_first():
    import pathlib
    html = (pathlib.Path(__file__).resolve().parents[1]/'app/static/index.html').read_text(encoding='utf-8')
    select = html.split('id="scan-mode"')[1].split('</select>')[0]
    assert select.index('value="pool"') < select.index('value="focus"')


def test_a_pool_scan_experiment_never_sells_a_name_because_it_left_the_list(desk):
    from test_month_desk import open_position
    open_position(desk, '105560', 60000.0, quantity=1)                  # bought earlier, e.g. on a pool signal
    pool_mode(desk)
    entry = {'picks': [{'symbol': '005930'}], 'session_date': '2026-10-06',
             'metrics': {'105560': {'last': 55000.0, 'sma20': 60000.0, 'ret_5d_pct': -8.0}}}
    with desk.store.edit() as s:
        desk.apply_rotation(s, 'KR', entry, set(), time.time())
    assert 'rotation' not in desk.store.read()['positions']['105560']
    pool_mode(desk, 'focus')                                              # the focus experiments keep the old rule
    with desk.store.edit() as s:
        desk.apply_rotation(s, 'KR', entry, set(), time.time())
    assert desk.store.read()['positions']['105560']['rotation']['action'] == 'sell'


def test_held_names_are_left_to_the_exit_rules_on_sell_signals():
    from app import gate
    v = gate.assess(held=True, signals={'momentum': 'SELL'}, last=None, price=100, now=0, sell_signals=False)
    assert (v['eligible'], v['reason']) == (False, 'holding')
    assert gate.assess(held=True, signals={'momentum': 'SELL'}, last=None, price=100, now=0)['eligible']        # focus experiments
    assert gate.assess(held=True, signals={'momentum': 'SELL'}, last=None, price=100, now=0, sell_signals=False, forced=True)['eligible']
    from app import rules
    from test_rule_board import series
    b = rules.board(series(1), held=True, sells_off=True)
    assert all(r['ignored'] for r in b['rules'])
