import pytest

from app.evaluation import record_decision, summarize, update_outcomes

T0 = 1_800_000_000


def quote(price, t, end=T0+6*3600, spread=0):
    return {'bid': price-spread, 'ask': price+spread, 'asof': t, 'received': t, 'session_end': end}


def state_with(stance, *, candidates=None, end=T0+6*3600, selected_by='ai'):
    s = {'quotes': {'AAA': quote(100, T0, end)}}
    record_decision(s, run_id='r', symbol='AAA', market='US', decision={'stance': stance, 'engine': 'Claude · opus'},
                    quote=quote(100, T0, end), candidates=candidates or {}, selected_by=selected_by,
                    cost_bps=20, now=T0)
    return s


def move_to(s, prices, t, end=T0+6*3600):
    s['quotes'] = {sym: quote(p, t, end) for sym, p in prices.items()}
    update_outcomes(s, t)


def test_horizons_fill_only_when_due_with_fresh_quotes():
    s = state_with('BUY', candidates={'BBB': 50})
    move_to(s, {'AAA': 101, 'BBB': 50}, T0+29*60)
    assert s['evaluations'][0]['outcomes'] == {}
    move_to(s, {'AAA': 101, 'BBB': 50.25}, T0+30*60+5)
    out = s['evaluations'][0]['outcomes']['30']
    assert out['returns'] == {'AAA': 1.0, 'BBB': 0.5} and not out['at_close']
    assert '60' not in s['evaluations'][0]['outcomes']


def test_stale_quote_is_not_used_and_long_gap_is_marked_missed():
    s = state_with('BUY')
    s['quotes']['AAA'] = quote(105, T0+60)            # old quote only
    update_outcomes(s, T0+30*60+5)
    assert '30' not in s['evaluations'][0]['outcomes']
    update_outcomes(s, T0+30*60+700)                  # app was down past the late limit
    assert s['evaluations'][0]['outcomes']['30'] == {'time': T0+30*60+700, 'missed': True}


def test_session_close_before_horizon_scores_at_close():
    end = T0+40*60
    s = state_with('BUY', end=end)
    move_to(s, {'AAA': 102}, T0+30*60+1, end)
    move_to(s, {'AAA': 99}, end-5, end)
    update_outcomes(s, end+3600)                      # market closed, quotes no longer fresh
    out = s['evaluations'][0]['outcomes']['60']
    assert out['at_close'] and out['returns']['AAA'] == -1.0


def scored(stance, ret, *, others=None, selected_by='ai'):
    s = state_with(stance, candidates={k: 100 for k in (others or {})}, selected_by=selected_by)
    prices = {'AAA': 100*(1+ret/100), **{k: 100*(1+v/100) for k, v in (others or {}).items()}}
    move_to(s, prices, T0+30*60+1)
    move_to(s, prices, T0+60*60+1)
    return s['evaluations'][0]


def test_summary_compares_ai_with_baselines():
    records = [scored('BUY', 1.0, others={'B': 0.1}), scored('BUY', -0.5), scored('SELL', -1.0),
               scored('HOLD', 0.8, others={'B': 2.0})]
    h = summarize(records)['horizons']['60']
    assert h['scored'] == 4
    assert h['buy'] == {'count': 2, 'avg_return_pct': 0.25, 'avg_net_pct': 0.05, 'hit_rate_pct': 50.0}
    assert h['sell']['avg_avoided_pct'] == 1.0 and h['sell']['hit_rate_pct'] == 100.0
    assert h['hold']['missed_gain_rate_pct'] == 100.0
    # BUY +0.8 net, BUY -0.7 net, SELL +0.8 net, HOLD 0  -> 0.225
    assert h['ai_avg_net_pct'] == pytest.approx(0.225)
    assert h['always_hold_pct'] == 0.0
    assert h['always_buy_avg_net_pct'] == pytest.approx((0.8-0.7-1.2+0.6)/4)
    assert h['selector'] == {'count': 2, 'chosen_abs_move_pct': 0.9, 'others_abs_move_pct': 1.05,
                             'bigger_mover_rate_pct': 50.0}


def test_small_sample_is_flagged():
    summary = summarize([scored('BUY', 1.0)])
    assert not summary['enough_sample'] and summary['engines'] == {'Claude': {'decisions': 1, 'trades': 1}}


def test_record_is_capped():
    s = {}
    for i in range(1005):
        record_decision(s, run_id=str(i), symbol='AAA', market='US', decision={'stance': 'HOLD'},
                        quote=quote(100, T0), candidates={}, selected_by='server', cost_bps=0, now=T0)
    assert len(s['evaluations']) == 1000 and s['evaluations'][0]['run_id'] == '5'
