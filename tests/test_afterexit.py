"""After-exit tracking: measurement of what happened after the desk sold (d5/d20) and, for take-profit exits, what holding
on with the same trailing stop (option C) would have earned versus selling at the target (option A). It never trades."""
import time

import pytest

from app import afterexit, verification
from test_month_desk import DAY, desk, open_position  # noqa: F401  (fixture)

T0 = 1_790_000_000.0


def bar(day, close, low=None, high=None, opening=None):
    return {'time': T0+day*DAY, 'close': close, 'low': low if low is not None else close, 'high': high if high is not None else close,
            'open': opening if opening is not None else close, 'completed': True}


def item(reason='익절 조건', **replay):
    plan = {'average': 100.0, 'stop_price': 104.0, 'trail_pct': 5.0, 'high_water': 110.0, 'expires_at': T0+40*DAY, **replay}
    x = {'id': 'x', 'symbol': '005930', 'time': T0+.5*DAY, 'reason': reason, 'exit_price': 110.0, 'style': 'trend', 'after': {}}
    if reason == '익절 조건':
        x['replay'] = plan
    return x


@pytest.mark.parametrize('signals, style', [({'momentum': 'BUY'}, 'trend'), ({'breakout': 'BUY', 'mean_reversion': 'HOLD'}, 'trend'),
                                            ({'mean_reversion': 'BUY'}, 'range'), ({'golden_cross': 'BUY', 'mean_reversion': 'BUY'}, 'mixed'),
                                            ({}, 'unknown'), (None, 'unknown'), ({'momentum': 'SELL'}, 'unknown')])
def test_the_setup_style(signals, style):
    assert afterexit.style_of(signals) == style


def test_c_follows_the_high_and_exits_on_the_trailing_stop():
    bars = [bar(1, 115, 112, 120), bar(2, 118, 116, 119), bar(3, 113, 113.5, 117, opening=116)]    # stop 120*0.95 = 114 is hit on day 3
    r = afterexit.replay_trailing(item(), bars, T0+4*DAY)
    assert r['why'] == '추적 손절' and r['exit_price'] == pytest.approx(114.0) and r['bars'] == 3


def test_a_bars_low_is_checked_before_its_high_raises_the_stop():
    # day 1: low 104.6 is above the starting stop 104.5 (110*0.95), high 130 then lifts the stop to 123.5 - not hit the same day
    r = afterexit.replay_trailing(item(), [bar(1, 125, 104.6, 130), bar(2, 122, 122, 126, opening=125)], T0+3*DAY)
    assert r['exit_price'] == pytest.approx(123.5) and r['bars'] == 2


def test_a_gap_below_the_stop_fills_at_the_open():
    r = afterexit.replay_trailing(item(), [bar(1, 99, 98, 101, opening=100)], T0+2*DAY)
    assert (r['why'], r['exit_price']) == ('추적 손절(갭)', 100)


def test_the_positions_own_expiry_still_ends_c():
    bars = [bar(1, 112), bar(2, 113), bar(3, 116)]
    r = afterexit.replay_trailing(item(expires_at=T0+2.5*DAY), bars, T0+4*DAY)
    assert (r['why'], r['exit_price'], r['bars']) == ('최대 보유시간', 113, 2)


def test_unsettled_c_waits_and_gives_up_only_long_after_the_expiry():
    assert afterexit.replay_trailing(item(), [bar(1, 112)], T0+2*DAY) is None
    assert afterexit.replay_trailing(item(), [], T0+62*DAY) == {'missed': True}
    assert 'skipped' in afterexit.replay_trailing(item(trail_pct=None), [], T0)


def test_update_fills_d5_d20_and_the_c_minus_a_difference():
    state = {'after_exits': [item(expires_at=T0+10.5*DAY), item('손절 조건')]}
    bars = [bar(0, 109)] + [bar(d, 110+d) for d in range(1, 21)]                # the exit day's own bar is not counted
    afterexit.update(state, {'005930': bars}, T0+22*DAY)
    take, stop = state['after_exits']
    assert take['after'] == {'d5': pytest.approx((115/110-1)*100, abs=1e-4), 'd20': pytest.approx((130/110-1)*100, abs=1e-4)}
    assert take['c'] == {'exit_price': 120, 'why': '최대 보유시간', 'bars': 10, 'extra_pct': pytest.approx((120/110-1)*100, abs=1e-4)}
    assert 'c' not in stop and stop['after']['d20'] is not None
    assert afterexit.due(state) == set()


def test_missing_bars_are_given_up_on_and_not_retried_forever():
    state = {'after_exits': [item('손절 조건')]}
    afterexit.update(state, {'005930': []}, T0+30*DAY)
    assert state['after_exits'][0]['after'] == {'d5': None}           # d20 still has time
    afterexit.update(state, {'005930': []}, T0+50*DAY)
    assert state['after_exits'][0]['after'] == {'d5': None, 'd20': None} and afterexit.due(state) == set()


def test_the_summary_splits_by_setup_style():
    a, b, c = item(), item(), item('손절 조건')
    a['c'], b['c'] = {'extra_pct': 4.0}, {'extra_pct': -2.0}
    b['style'], c['style'] = 'range', 'range'
    a['after'], c['after'] = {'d5': 1.0, 'd20': 3.0}, {'d5': -1.0}
    out = afterexit.summary({'after_exits': [a, b, c]})
    rows = {r['style']: r for r in out['rows']}
    assert rows['trend']['c_minus_a_avg_pct'] == 4.0 and rows['trend']['c_better'] == 1
    assert rows['range']['exits'] == 2 and rows['range']['c_minus_a_avg_pct'] == -2.0 and rows['range']['d5_avg_pct'] == -1.0
    assert out['tracked'] == 3


# ---- with the desk ------------------------------------------------------------------------------------------------------------

def test_a_take_profit_exit_starts_tracking_with_its_trailing_plan(desk):
    with desk.store.edit() as s:
        s.setdefault('evaluations', []).append({'symbol': '005930', 'time': time.time()-5, 'selected_by': 'server', 'stance': 'BUY',
                                 'rules': {'momentum': 'BUY', 'mean_reversion': 'HOLD'}, 'outcomes': {}, 'horizon': 'month',
                                 'engine': 'claude', 'market': 'KR', 'price': 100000.0, 'cost_bps': 10.0, 'action': 'filled',
                                 'candidates': {}, 'run_id': 'r', 'target_weight_pct': 10})
    position = open_position(desk)
    desk.provider.prices['005930'] = position['take_profit_price']+10
    desk.process_desk_exits()
    state = desk.store.read()
    x = state['after_exits'][-1]
    assert (x['reason'], x['style'], x['entry']) == ('익절 조건', 'trend', 'analysis')
    assert x['replay']['trail_pct'] == 5 and x['replay']['expires_at'] == position['expires_at']
    assert x['exit_price'] == state['trades'][-1]['price'] and x['trip_return_pct'] > 0
    assert afterexit.due(state) == {'005930'}
    public = desk.public_state()
    assert 'after_exits' not in public and public['after_exit']['tracked'] == 1


def test_a_stop_exit_is_tracked_without_a_replay_and_a_partial_sell_is_not(desk):
    position = open_position(desk)
    desk.provider.prices['005930'] = position['stop_price']-1
    desk.process_desk_exits()
    x = desk.store.read()['after_exits'][-1]
    assert x['reason'] == '손절 조건' and 'replay' not in x and x['style'] == 'unknown'


def test_the_daily_job_reads_bars_for_tracked_exits_and_fills_them(desk, monkeypatch):
    position = open_position(desk)
    desk.provider.prices['005930'] = position['stop_price']-1
    desk.process_desk_exits()
    sold = desk.store.read()['after_exits'][-1]['time']
    rows = [{'time': sold+d*DAY, 'close': 90000.0+d, 'open': 90000.0, 'high': 90010.0, 'low': 89990.0, 'completed': True}
            for d in range(1, 21)]
    asked = []
    monkeypatch.setattr(desk, 'daily_bars', lambda symbol, now: asked.append(symbol) or rows)
    desk.days_scored_at = 0
    desk.score_days(now=sold+25*DAY)
    assert '005930' in asked and desk.store.read()['after_exits'][-1]['after']['d20'] is not None


def test_tracking_is_not_part_of_the_strategy_fingerprint():
    import inspect
    assert 'afterexit' not in inspect.getsource(verification)


def test_a_conditional_entry_takes_its_setup_from_the_analysis_that_left_the_plan():
    state = {'evaluations': [{'symbol': '005930', 'time': 10, 'selected_by': 'server', 'rules': {'mean_reversion': 'BUY'}},
                             {'symbol': '005930', 'time': 20, 'selected_by': 'watch', 'rules': {}}],
             'watches': [{'id': 'w1', 'type': 'pullback'}]}
    assert afterexit._setup(state, '005930', 20, {'entry_watch': 'w1'}) == {'style': 'range', 'signals': {'mean_reversion': 'BUY'},
                                                                          'entry': 'pullback'}
