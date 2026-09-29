"""Rule-based baselines and how they are scored next to the AI."""
import pytest

from app import rules
from app.evaluation import record_decision, summarize, update_outcomes

T0 = 1_800_000_000


def bars(closes, spread=0.05):
    return [{'close': c, 'high': c+spread, 'low': c-spread} for c in closes]


def wave(*parts):
    out = []
    for start, stop, count in parts:
        step = (stop-start)/count
        out += [start+step*(i+1) for i in range(count)]
    return out


def test_golden_cross_fires_on_an_upward_and_a_downward_cross():
    falling = [100.0]*20+[100.0-i for i in range(1, 11)]
    rebound = falling+[falling[-1]+6*k for k in range(1, 5)]
    seen = [rules.golden_cross(bars(rebound[:n])) for n in range(len(falling), len(rebound)+1)]
    assert seen[0] == 'HOLD' and 'BUY' in seen and 'SELL' not in seen
    rising = [100.0]*20+[100.0+i for i in range(1, 11)]
    fall = rising+[rising[-1]-6*k for k in range(1, 5)]
    seen = [rules.golden_cross(bars(fall[:n])) for n in range(len(rising), len(fall)+1)]
    assert seen[0] == 'HOLD' and 'SELL' in seen and 'BUY' not in seen
    assert rules.golden_cross(bars([100.0]*40)) == 'HOLD' and rules.golden_cross(bars([100.0]*22)) is None


def test_momentum_is_scaled_by_noise_so_price_level_does_not_matter():
    up = [100+0.3*i+(0.05 if i % 2 else -0.05) for i in range(30)]
    down = [200-0.6*i+(0.1 if i % 2 else -0.1) for i in range(30)]
    choppy = [100+(0.5 if i % 2 else -0.5) for i in range(30)]
    assert rules.momentum(bars(up)) == 'BUY' and rules.momentum(bars(down)) == 'SELL'
    assert rules.momentum(bars(choppy)) == 'HOLD'
    assert rules.momentum(bars([100.0]*30)) is None and rules.momentum(bars(up[:10])) is None
    assert rules.momentum(bars([0.0]+[1.0]*25)) is None


def test_mean_reversion_fades_a_stretch_in_either_direction():
    calm = [100+(0.1 if i % 2 else -0.1) for i in range(19)]
    assert rules.mean_reversion(bars(calm+[103.0])) == 'SELL'
    assert rules.mean_reversion(bars(calm+[97.0])) == 'BUY'
    assert rules.mean_reversion(bars(calm+[100.05])) == 'HOLD'
    assert rules.mean_reversion(bars([100.0]*20)) == 'HOLD' and rules.mean_reversion(bars(calm[:10])) is None


def test_breakout_needs_a_close_beyond_the_prior_twenty_bars():
    base = [100+(0.2 if i % 2 else -0.2) for i in range(20)]
    assert rules.breakout(bars(base+[101.0])) == 'BUY'
    assert rules.breakout(bars(base+[99.0])) == 'SELL'
    assert rules.breakout(bars(base+[100.1])) == 'HOLD'
    assert rules.breakout(bars(base)) is None
    assert rules.breakout([{'close': 1.0}]*25) is None


def test_signals_returns_every_rule_and_survives_bad_data():
    everything = rules.signals(bars([100.0+i*0.1 for i in range(40)]))
    assert set(everything) == set(rules.RULES) and all(v in ('BUY', 'SELL', 'HOLD') for v in everything.values())
    assert rules.signals([{'close': 'x'}]*30) == {name: None for name in rules.RULES}
    assert rules.signals([]) == {name: None for name in rules.RULES}


# ---- scored next to the AI ---------------------------------------------------------------------------------------
def quote(price, t):
    return {'bid': price, 'ask': price, 'asof': t, 'received': t, 'session_end': T0+6*3600}


def decision(state, stance, moves, rule_signals, cost_bps=20):
    record_decision(state, run_id=str(len(state.get('evaluations', []))), symbol='AAA', market='US',
                    decision={'stance': stance, 'engine': 'Claude'}, quote=quote(100, T0), candidates={},
                    selected_by='ai', cost_bps=cost_bps, now=T0, rules=rule_signals)
    entry = state['evaluations'][-1]
    entry['outcomes'] = {h: {'time': T0, 'returns': {'AAA': moves}} for h in ('30', '60')}
    return entry


def test_rules_are_scored_on_the_same_moments_and_costs_as_the_ai():
    state = {}
    decision(state, 'BUY', 1.0, {'golden_cross': 'BUY', 'momentum': 'SELL', 'mean_reversion': 'HOLD', 'breakout': None})
    decision(state, 'HOLD', -1.0, {'golden_cross': 'SELL', 'momentum': 'SELL', 'mean_reversion': 'BUY', 'breakout': 'BUY'})
    decision(state, 'BUY', 0.1, {'golden_cross': 'BUY', 'momentum': 'HOLD', 'mean_reversion': None, 'breakout': 'HOLD'})
    rows = summarize(state['evaluations'])['horizons']['60']['rules']
    # golden cross: +0.8 (BUY on +1.0), +0.8 (SELL on -1.0), -0.1 (BUY on +0.1 with 0.2 costs)
    assert rows['golden_cross']['count'] == 3 and rows['golden_cross']['trades'] == 3
    assert rows['golden_cross']['avg_net_pct'] == pytest.approx((0.8+0.8-0.1)/3, abs=1e-3)
    assert rows['golden_cross']['hit_rate_pct'] == pytest.approx(66.7, abs=0.1)
    # the AI over the same three moments: BUY +0.8, HOLD 0, BUY -0.1
    assert rows['golden_cross']['ai_same_avg_net_pct'] == pytest.approx((0.8+0-0.1)/3, abs=1e-3)
    # momentum: SELL on +1.0 loses, SELL on -1.0 wins, HOLD earns nothing
    assert rows['momentum']['avg_net_pct'] == pytest.approx((-1.2+0.8+0)/3, abs=1e-3)
    # a rule that could not be computed is left out of that decision only
    assert rows['mean_reversion']['count'] == 2 and rows['breakout']['count'] == 2
    assert rows['mean_reversion']['ai_same_avg_net_pct'] == pytest.approx((0.8+0)/2, abs=1e-3)


def test_rules_with_no_usable_signal_report_zero_counts_and_old_records_still_summarise():
    state = {}
    entry = decision(state, 'BUY', 0.5, {name: None for name in rules.RULES})
    entry.pop('rules')                                                  # a record written before rules existed
    rows = summarize(state['evaluations'])['horizons']['60']['rules']
    assert all(row['count'] == 0 and row['avg_net_pct'] is None for row in rows.values())


def test_recording_stores_the_rule_signals_for_later_scoring():
    state = {'quotes': {'AAA': quote(100, T0)}}
    record_decision(state, run_id='r', symbol='AAA', market='US', decision={'stance': 'HOLD'}, quote=quote(100, T0),
                    candidates={}, selected_by='ai', cost_bps=20, now=T0, rules={'momentum': 'BUY'})
    assert state['evaluations'][0]['rules'] == {'momentum': 'BUY'}
    state['quotes'] = {'AAA': quote(101, T0+30*60+2)}                   # a fresh quote at the horizon
    update_outcomes(state, T0+30*60+5)                                  # scoring still works with the new field
    assert state['evaluations'][0]['outcomes']['30']['returns'] == {'AAA': 1.0}
