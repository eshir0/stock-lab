"""Stock splits (2026-10-07): a held position, a waiting plan and the scores follow a split instead of reading it as a
crash. NVDA and AVGO, both in the pool, split 10:1 in 2024."""
import time

import pytest

from app import scorecard, splits
from app.evaluation import score_days
from test_month_desk import DAY, bars, desk, open_position, run_cycle, series  # noqa: F401  (fixture)


def test_the_band_and_the_ratios():
    assert splits.suspicious('AAPL', 20.0, 200.0) and splits.suspicious('AAPL', 400.0, 200.0)
    assert not splits.suspicious('AAPL', 150.0, 200.0)                      # -25% in a day can happen
    assert splits.suspicious('005930', 60000, 100000) and not splits.suspicious('005930', 72000, 100000)   # KR +-30% limit
    assert splits.snap(10.03) == 10 and splits.snap(1.49) == 1.5 and splits.snap(0.1) == 0.1 and splits.snap(4) == 4
    assert splits.snap(1.1) is None and splits.snap(7.4) is None and splits.snap(None) is None


def test_the_ratio_comes_from_the_adjusted_bar_of_the_reference_day():
    ref = {'time': 1000, 'close': 200.0}
    assert splits.from_bars(ref, [{'time': 1000, 'close': 20.0}]) == 10
    assert splits.from_bars(ref, [{'time': 1000, 'close': 199.0}]) == 1.0        # not adjusted: no split
    assert splits.from_bars(ref, [{'time': 999, 'close': 20.0}]) is None         # that day is missing
    pack = {'splits': [{'ex_date': '2026-06-10', 'ratio': 10.0}]}
    assert splits.from_pack(pack, 'NVDA', 0) == 10 and splits.from_pack(pack, 'NVDA', time.time()) is None


def _held(symbol='AAPL', qty=2, price=200.0, fractional=True):
    trades = [{'id': 'b', 'time': 100, 'symbol': symbol, 'side': 'BUY', 'quantity': qty, 'price': price, 'fee': 0.2}]
    return {'positions': {symbol: {'quantity': qty, 'average': price, 'cost_basis': qty*price, 'stop_price': price*.9,
                                   'take_profit_price': price*1.3, 'high_water': price*1.05, 'opened': 100, 'trail_pct': 10}},
            'trades': trades, 'cash': {'KRW': 0, 'USD': 0}, 'proposals': [],
            'watches': [{'symbol': symbol, 'status': 'waiting', 'created': 50, 'level': price*.95, 'invalidate': price*.9,
                         'reference': price}]}


def test_applying_a_split_keeps_the_money_and_rescales_shares_and_prices():
    state = _held()
    text = splits.apply(state, 'AAPL', 10, 500, 'detected', True)
    pos = state['positions']['AAPL']
    assert pos['quantity'] == 20 and pos['average'] == 20 and pos['stop_price'] == 18 and pos['high_water'] == 21
    assert pos['trail_pct'] == 10 and pos['cost_basis'] == 400                       # percentages and money unchanged
    assert state['trades'][0]['quantity'] == 20 and state['trades'][0]['price'] == 20
    assert state['watches'][0]['level'] == 19 and state['watches'][0]['invalidate'] == 18
    assert '2주 → 20주' in text and splits.apply(state, 'AAPL', 10, 600, 'archive', True) is None   # same day: once
    _, open_trips = scorecard.round_trips(state['trades'])
    assert open_trips[0]['held'] == 20 and float(open_trips[0]['invested']) == pytest.approx(400.2)
    assert splits.factor(state, 'AAPL', 400) == 10 and splits.factor(state, 'AAPL', 501) == 1


def test_a_korean_fraction_is_settled_in_cash_and_the_ledger_still_balances():
    state = _held('005930', qty=3, price=90000.0, fractional=False)
    splits.apply(state, '005930', 1.5, 500, 'detected', False)
    pos = state['positions']['005930']
    assert pos['quantity'] == 4 and state['cash']['KRW'] == 30000                     # 4.5 shares: 0.5 x 60,000 in cash
    _, open_trips = scorecard.round_trips(state['trades'])
    assert open_trips[0]['held'] == 4


def test_a_decision_before_a_split_is_scored_on_the_adjusted_bars():
    now = time.time()
    rows = [{'time': now-30*DAY+i*DAY, 'close': 21.0, 'completed': True} for i in range(30)]
    state = {'evaluations': [{'symbol': 'AAPL', 'horizon': 'month', 'time': now-25*DAY-60, 'price': 200.0, 'outcomes': {}}],
             'splits': {'AAPL': [{'date': 'x', 'ratio': 10, 'time': now-20*DAY}]}}
    score_days(state, now, {'AAPL': rows})
    assert state['evaluations'][0]['outcomes']['d5']['returns']['AAPL'] == pytest.approx(5.0)    # 20 -> 21, not -89.5%


# ---- the engine -------------------------------------------------------------------------------------------------------------

def _split_day(desk, price_before, price_after, adjusted):
    pos = open_position(desk, 'AAPL', price=price_before, stop_pct=10, quantity=1)
    rows = bars([price_before]*70)
    with desk.store.edit() as s:
        s['positions']['AAPL']['ref_close'] = {'time': rows[-1]['time'], 'close': price_before, 'read': time.time()}
    desk.provider.daily['AAPL'] = bars([price_before/10 if adjusted else price_before]*70)
    desk.provider.prices['AAPL'] = price_after
    desk.refresh()
    return pos


def test_a_confirmed_split_is_applied_and_nothing_is_sold(desk):
    _split_day(desk, 200.0, 20.0, adjusted=True)
    desk.check_splits()
    desk.process_desk_exits()
    state = desk.store.read()
    pos = state['positions']['AAPL']
    assert pos['quantity'] == 10 and pos['stop_price'] == pytest.approx(18) and not any(t['side'] == 'SELL' for t in state['trades'])
    assert any('주식 분할(10:1' in e['message'] for e in state['events']) and state['splits']['AAPL'][0]['ratio'] == 10


def test_a_real_crash_is_not_mistaken_for_a_split(desk):
    _split_day(desk, 200.0, 20.0, adjusted=False)                   # the bars show yesterday's close unchanged
    desk.check_splits()
    desk.process_desk_exits()
    state = desk.store.read()
    assert 'AAPL' not in state['positions'] and not state.get('split_checks')
    assert any('실제 움직임' in e['message'] for e in state['events'])


def test_an_unconfirmed_move_holds_the_stop_until_the_data_tells_or_time_runs_out(desk, monkeypatch):
    _split_day(desk, 200.0, 20.0, adjusted=False)
    desk.provider.daily['AAPL'] = [b for b in bars([20.0]*70)][:-1]          # the reference day is missing: cannot tell
    with desk.store.edit() as s:
        s['positions']['AAPL']['ref_close']['time'] = 1                       # a day the bars do not have
    desk.check_splits()
    desk.process_desk_exits()
    state = desk.store.read()
    assert state['positions']['AAPL']['quantity'] == 1 and 'AAPL' in state['split_checks']   # held back, not sold
    desk.check_splits(now=time.time()+splits.GIVE_UP+600)
    assert not desk.store.read().get('split_checks')
    desk.process_desk_exits()
    assert 'AAPL' not in desk.store.read()['positions']                      # then the stop applies as usual


def test_the_archive_record_registers_a_split_for_scoring(desk, tmp_path):
    import json
    from app.evidence import EvidenceStore
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    ex = (datetime.now(ZoneInfo('America/New_York'))-timedelta(days=1)).date().isoformat()
    with desk.store.edit() as s:
        s['started_at'] = time.time()-10*DAY
        s.setdefault('evaluations', []).append({'symbol': 'MSFT', 'horizon': 'month', 'time': time.time()-5*DAY, 'price': 400.0, 'outcomes': {}})
    pack = {'symbol': 'MSFT', 'name': 'x', 'market': 'US', 'as_of_bar': datetime.now().date().isoformat(),
            'built_at': datetime.now().isoformat(timespec='seconds'), 'splits': [{'ex_date': ex, 'ratio': 4.0}]}
    (tmp_path/'MSFT.json').write_text(json.dumps(pack))
    desk.evidence = EvidenceStore(tmp_path)
    desk.register_splits()
    state = desk.store.read()
    assert state['splits']['MSFT'][0]['ratio'] == 4 and splits.factor(state, 'MSFT', time.time()-5*DAY) == 4


def test_the_split_moment_never_trips_the_daily_loss_limit_or_the_drawdown(desk):
    _split_day(desk, 200.0, 20.0, adjusted=True)
    state = desk.store.read()
    assert not state['risk_status']['halted'] and 'AAPL' in state['split_quotes']        # the -90% quote was kept aside
    assert state['quotes']['AAPL']['last'] == 200.0
    desk.check_splits()
    desk.refresh()
    state = desk.store.read()
    assert not state['risk_status']['halted'] and state['quotes']['AAPL']['last'] == 20.0
    assert not any('일일 손실 한도' in e['message'] for e in state['events'])
    assert state['performance']['USD']['max_drawdown_pct'] < 5


def test_a_pullback_plan_is_not_triggered_by_a_split(desk):
    from app import entry
    desk.provider.prices['MSFT'] = 400.0
    desk.refresh()
    with desk.store.edit() as s:
        s.setdefault('watches', []).append({'id': 'w1', 'symbol': 'MSFT', 'name': 'Microsoft', 'market': 'US', 'currency': 'USD',
                                            'horizon': 'month', 'type': 'pullback', 'status': 'waiting', 'created': time.time()-60,
                                            'expires': time.time()+3600, 'level': 390.0, 'invalidate': 370.0, 'reference': 400.0,
                                            'hits': 0, 'last_received': 0, 'volume_after': 0, 'plan': {}})
    adjusted = bars([100.0]*70)                                         # adjusted 4:1, yesterday 400 -> 100
    desk.provider.daily['MSFT'] = adjusted
    desk.provider.prices['MSFT'] = 99.0
    desk.daily_cache.pop('MSFT', None)
    with desk.store.edit() as s:                                        # the last quote seen was yesterday's session
        s['quotes']['MSFT'].update(asof=adjusted[-1]['time']+15*3600, received=adjusted[-1]['time']+15*3600,
                                   book_asof=adjusted[-1]['time']+15*3600)
    desk.refresh()
    assert desk.store.read()['quotes']['MSFT']['last'] == 400.0          # held aside, the plan cannot see 99 yet
    desk.process_entry_watches()
    assert entry.waiting(desk.store.read())[0]['hits'] == 0
    desk.check_splits()                                                 # yesterday's 400 is 100 in the adjusted bars
    state = desk.store.read()
    plan = entry.waiting(state)[0]
    assert plan['level'] == 97.5 and plan['invalidate'] == 92.5 and state['quotes']['MSFT']['last'] == 99.0
    desk.process_entry_watches()
    assert entry.waiting(desk.store.read())[0]['hits'] == 0             # 99 is above the adjusted 97.5: no buy
