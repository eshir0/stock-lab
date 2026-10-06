"""The verification plan and the trade report card: round trips from the ledger, their statistics with an honest confidence
interval, an index ETF simply held as the benchmark, the rule-follow baseline the AI is scored against, and the fixed plan
with its verdict. Plain dicts plus throw-away ledgers; no market data or AI service is contacted."""
import time

import pytest

from app import evaluation, scorecard, verification
from app.config import Config
from app.performance import record_performance
from app.scorecard import benchmark_entry, mean_ci, report, round_trips, t95
from test_month_desk import bars, desk, run_cycle, series          # noqa: F401  (desk is a fixture)

DAY = 86400
NOW = 1_800_000_000.0


# ---- statistics ---------------------------------------------------------------------------------------------------------

def test_the_confidence_interval_is_never_surer_than_the_sample_allows():
    assert t95(1) == 12.706 and t95(11) == 2.228 and t95(119) == 2.0 and t95(200) == 1.96 and t95(0) is None
    mean, low, high = mean_ci([1, 2, 3])
    assert mean == 2 and low == pytest.approx(2-4.303/3**.5, abs=1e-3) and high == pytest.approx(2+4.303/3**.5, abs=1e-3)
    assert mean_ci([5]) == (5, None, None) and mean_ci([]) == (None, None, None)
    assert mean_ci([1.0]*40)[1:] == (1.0, 1.0)                        # no spread: no doubt


# ---- round trips --------------------------------------------------------------------------------------------------------

def fill(t, symbol, side, qty, price, fee=0.0, realized=0.0, **extra):
    return {'time': t, 'symbol': symbol, 'side': side, 'quantity': qty, 'price': price, 'fee': fee, 'realized': realized, **extra}


TRADES = [
    fill(1*DAY, '005930', 'BUY', 10, 100.0, fee=1.0, origin='ai', reused=True),
    fill(2*DAY, '005930', 'SELL', 4, 110.0, realized=38.0, exit_reason='익절 조건'),
    fill(3*DAY, '005930', 'SELL', 6, 90.0, realized=-61.0, exit_reason='손절 조건'),        # closes the first trip: -23 on 1,001
    fill(4*DAY, '122630', 'BUY', 1, 1000.0, fee=2.0, entry_watch='w1'),
    fill(6*DAY, '122630', 'SELL', 1, 1100.0, realized=96.0),                                   # +96 on 1,002
    fill(7*DAY, 'AAPL', 'BUY', .5, 200.0, fee=0.0),                                            # still open
    fill(8*DAY, 'MSFT', 'SELL', 1, 400.0, realized=5.0),                                       # a sale with no recorded buy
    fill(9*DAY, 'NOPE', 'BUY', 1, 1.0),
]


def test_round_trips_run_from_the_first_buy_to_the_sale_that_leaves_nothing():
    closed, still = round_trips(TRADES)
    assert [t['symbol'] for t in closed] == ['005930', '122630'] and [t['symbol'] for t in still] == ['AAPL']
    first, second = closed
    assert first['pnl'] == -23.0 and first['return_pct'] == pytest.approx(-23/1001*100, abs=1e-4) and first['days'] == 2
    assert (first['origin'], first['reused'], first['leveraged'], first['exit_reason']) == ('ai', True, False, '손절 조건')
    assert (second['origin'], second['reused'], second['leveraged']) == ('watch', False, True)
    assert second['return_pct'] == pytest.approx(96/1002*100, abs=1e-4)


def test_the_report_card():
    card = report(TRADES)
    assert (card['closed'], card['open'], card['wins'], card['losses']) == (2, 1, 1, 1)
    assert card['win_rate_pct'] == 50.0 and card['expectancy_pct'] == pytest.approx((-23/1001+96/1002)*50, abs=1e-3)
    assert card['payoff'] == pytest.approx((96/1002)/(23/1001), abs=1e-2)
    assert card['profit_factor'] == pytest.approx((96/1002)/(23/1001), abs=1e-2)
    assert card['pnl'] == {'KRW': 73.0} and card['avg_days'] == 2.0
    assert card['ci_pct'][0] < card['expectancy_pct'] < card['ci_pct'][1]
    groups = card['groups']
    assert groups['leveraged']['count'] == 1 and groups['plain']['count'] == 1 and groups['watch']['count'] == 1
    assert groups['analysis']['count'] == 1 and groups['reused']['count'] == 1 and groups['fresh']['count'] == 1
    assert [t['symbol'] for t in card['recent']] == ['122630', '005930']                       # newest first
    empty = report([])
    assert empty['closed'] == 0 and empty['expectancy_pct'] is None and empty['ci_pct'] is None and empty['payoff'] is None


def test_an_even_round_trip_counts_as_a_loss_and_profit_factor_needs_a_loss():
    card = report([fill(1, 'AAPL', 'BUY', 1, 100.0), fill(2, 'AAPL', 'SELL', 1, 100.0, realized=0.0)])
    assert card['losses'] == 1 and card['profit_factor'] is None and card['payoff'] is None


# ---- the benchmark --------------------------------------------------------------------------------------------------------

def daily(closes, first=NOW-10*DAY):
    return [{'time': first+i*DAY, 'close': c} for i, c in enumerate(closes)]


def test_the_benchmark_is_an_index_etf_bought_at_the_start_and_held():
    rows = daily([100, 101, 102, 110, 99, 105])                      # the start falls between the 3rd and the 4th bar
    entry = benchmark_entry('069500', rows, NOW-8*DAY+60)
    assert entry['start_close'] == 102 and entry['last_close'] == 105 and entry['return_pct'] == pytest.approx(2.941, abs=1e-3)
    assert entry['max_drawdown_pct'] == pytest.approx((110-99)/110*100, abs=1e-3) and entry['name'] == 'KODEX 200'
    later = benchmark_entry('069500', rows+daily([120], first=NOW-4*DAY), NOW-8*DAY+60, entry)  # the starting close never moves
    assert later['start_close'] == 102 and later['return_pct'] == pytest.approx(120/102*100-100, abs=1e-3)
    assert benchmark_entry('069500', rows, NOW-20*DAY) is None                                 # nothing before the start yet
    assert benchmark_entry('SPY', [], NOW, None) is None and benchmark_entry('SPY', [], NOW, entry) == entry
    fresh = benchmark_entry('069500', rows[:3], NOW-8*DAY+60)
    assert fresh['return_pct'] == 0 and fresh['last_close'] == 102


# ---- the plan and its verdict ------------------------------------------------------------------------------------------------

CFG = Config(mode='demo', password='x'*8, session_secret='y'*32)


def judge(days=60, mine=5.0, index=2.0, index_dd=4.0, drawdown=3.0, expectancy=.8, ci=(.2, 1.4), closed=30, decisions=120, groups=None,
          changes=(10_000,)*5, plan=None, **extra):
    state = {'verification': plan or verification.start(CFG, NOW-days*DAY), 'initial': {'KRW': 1_000_000, 'USD': 0},
             'performance': {'KRW': {'return_pct': mine, 'max_drawdown_pct': drawdown}},
             'benchmark': {'KRW': {'name': 'KODEX 200', 'return_pct': index, 'max_drawdown_pct': index_dd}} if index is not None else {}}
    equity, values = 1_000_000, {}
    for i, change in enumerate(changes):
        equity += change
        values[f'2026-08-{i+1:02d}'] = equity
    card = {'closed': closed, 'expectancy_pct': expectancy, 'ci_pct': list(ci) if ci else None, 'groups': groups or {}}
    ev = {'horizons': {'d5': {'scored': decisions, 'ai_vs_rule': {'count': 3}}}}
    return verification.judge(state, tracking={'KRW': {'daily': values}}, report=card, evaluation=ev, config=extra.get('config', CFG),
                              now=NOW)


def test_the_plan_is_fixed_at_the_start_with_a_fingerprint_of_the_strategy():
    plan = verification.start(CFG, NOW)
    assert plan['criteria'] == verification.CRITERIA and plan['started_at'] == NOW and plan['version'] == verification.STRATEGY_VERSION
    assert plan['fingerprint'] == verification.fingerprint(CFG) and len(plan['fingerprint']) == 16
    assert verification.fingerprint(Config(mode='demo', password='x'*8, session_secret='y'*32, fee_kr=15)) != plan['fingerprint']
    assert verification.fingerprint(Config(mode='demo', password='z'*9, session_secret='w'*33)) == plan['fingerprint']   # not a secret's business


def test_a_strategy_that_clears_every_bar_passes():
    verdict = judge()
    assert verdict['status'] == 'pass' and verdict['ready'] and not verdict['strategy_changed']
    assert {c['key'] for c in verdict['checks']} == {'expectancy', 'benchmark_KRW', 'drawdown_KRW', 'best_days_KRW'}
    assert all(c['ok'] for c in verdict['checks']) and verdict['ai_vs_rule'] == {'count': 3}
    assert [p['key'] for p in verdict['progress']] == ['days', 'trades', 'decisions']


@pytest.mark.parametrize('over, failing', [
    ({'index': 6.0}, 'benchmark_KRW'),                         # holding the index earned more
    ({'drawdown': 5.5}, 'drawdown_KRW'),                       # deeper than the index's 4%
    ({'ci': (-.1, 1.7)}, 'expectancy'),                        # positive on average, but not surely
    ({'expectancy': -.2, 'ci': (-.9, .5)}, 'expectancy'),
    ({'changes': (100_000, -10_000, -10_000, 5_000, 5_000)}, 'best_days_KRW'),   # one lucky day carried it
    ({'groups': {'leveraged': {'count': 4}, 'plain': {'expectancy_pct': -.3}}}, 'without_leverage'),
])
def test_one_missed_bar_fails_the_strategy(over, failing):
    verdict = judge(**over)
    assert verdict['status'] == 'fail' and [c['key'] for c in verdict['checks'] if c['ok'] is False] == [failing]


@pytest.mark.parametrize('over', [{'days': 30}, {'closed': 29}, {'decisions': 99}])
def test_nothing_is_decided_before_the_period_and_the_samples_are_complete(over):
    assert judge(**over)['status'] == 'collecting'
    assert judge(index=9.0, **over)['status'] == 'collecting'                                 # not even a failure


def test_a_check_that_cannot_be_computed_yet_keeps_it_collecting():
    assert judge(index=None)['status'] == 'collecting'
    assert judge(ci=None)['status'] == 'collecting'
    assert judge(changes=(1, 2, 3))['status'] == 'collecting'                                 # three days are not enough to drop three
    assert judge(index=None, ci=(-.5, .1))['status'] == 'fail'                               # but a known failure is a failure


def test_a_change_of_strategy_halfway_is_flagged():
    plan = verification.start(Config(mode='demo', password='x'*8, session_secret='y'*32, fee_kr=15), NOW-60*DAY)
    assert judge(plan=plan)['strategy_changed'] and not judge()['strategy_changed']


def test_an_experiment_without_a_plan_has_no_verdict():
    assert verification.judge({}, tracking={}, report={}, evaluation={}, config=CFG, now=NOW) is None


def test_daily_changes_start_from_the_seed():
    assert verification.daily_changes({'2026-08-02': 120, '2026-08-01': 110}, 100) == [10, 10]


# ---- the rule-follow baseline ----------------------------------------------------------------------------------------------

def scored(stance, move, trigger, cost=40):
    entry = evaluation.record_decision({}, run_id='r', symbol='A1', market='KR', decision={'stance': stance}, quote={'bid': 100, 'ask': 100},
                                       candidates={}, selected_by='ai', cost_bps=cost, now=NOW, horizon='month', trigger_side=trigger)
    entry['outcomes']['d5'] = {'returns': {'A1': move}}
    return entry


def test_the_ai_is_scored_against_simply_following_the_rule_that_started_the_analysis():
    records = [scored('HOLD', 2.0, 'BUY'), scored('BUY', -1.0, 'BUY'), scored('HOLD', -3.0, 'SELL'), scored('BUY', 1.0, None)]
    assert 'trigger_side' not in records[-1]
    row = evaluation.summarize(records)['horizons']['d5']['ai_vs_rule']
    # AI: 0, -1.4, 0 ; rule: +1.6, -1.4, +2.6 (a sell avoids the fall, less the cost)
    assert row['count'] == 3 and row['ai_avg_net_pct'] == pytest.approx(-1.4/3, abs=1e-3)
    assert row['rule_avg_net_pct'] == pytest.approx((1.6-1.4+2.6)/3, abs=1e-3)
    assert row['diff_avg_pct'] == pytest.approx((-1.6+0-2.6)/3, abs=1e-3) and row['diff_ci_pct'][0] < row['diff_avg_pct']
    assert evaluation.summarize([])['horizons']['d5']['ai_vs_rule']['count'] == 0


def test_a_month_analysis_started_by_a_rule_records_what_the_rule_said(desk):
    desk.provider.daily['005930'] = bars(series(70000, 'up'))
    state = run_cycle(desk)
    assert state['evaluations'][-1]['trigger_side'] == 'BUY'
    assert state['trades'][-1]['origin'] == 'ai' and not state['trades'][-1].get('reused')
    desk.request_cycle('000660')                                                              # the owner's request is not a rule
    desk.cycle()
    assert 'trigger_side' not in desk.store.read()['evaluations'][-1]


# ---- on a ledger ----------------------------------------------------------------------------------------------------------

def test_a_new_month_experiment_fixes_its_plan_and_the_page_gets_the_verdict_and_the_report_card(desk):
    state = desk.store.read()
    assert state['verification']['fingerprint'] == verification.fingerprint(desk.c)
    page = desk.public_state()
    assert page['verification']['status'] == 'collecting' and page['scorecard']['closed'] == 0
    desk.stop()
    desk.new_experiment(1000, 1000, 'basic', strategy_mode='legacy')
    assert 'verification' not in desk.store.read() and desk.public_state()['verification'] is None


def test_the_benchmark_is_read_at_most_every_fifteen_minutes_and_starts_fresh_with_a_new_experiment(desk):
    desk.refresh_benchmark(now=time.time())
    bench = desk.store.read()['benchmark']
    assert set(bench) == {'KRW', 'USD'} and bench['KRW']['symbol'] == '069500' and bench['USD']['symbol'] == 'SPY'
    calls = []
    desk.daily_bars = lambda symbol, now=None: calls.append(symbol) or []
    desk.refresh_benchmark(now=time.time())
    assert calls == []
    desk.refresh_benchmark(now=time.time()+901)
    assert sorted(calls) == ['069500', 'SPY']
    desk.stop()
    desk.new_experiment(1_000_000, 1000, 'next', strategy_mode='intraday', strategy_settings={'horizon': 'month', 'universe_mode': 'fixed'})
    assert 'benchmark' not in desk.store.read()


def test_the_equity_of_each_day_is_kept_for_far_longer_than_the_minute_history():
    state = {'cash': {'KRW': 100.0, 'USD': 10.0}, 'positions': {}, 'quotes': {}, 'initial': {'KRW': 100.0, 'USD': 10.0}}
    for day in range(scorecard_days := 410):
        record_performance(state, now=NOW+day*DAY, force=True)
    daily = state['performance']['KRW']['daily']
    assert len(daily) == 400 and min(daily) > '2027' and all(v == 100.0 for v in daily.values())
    assert scorecard_days == 410 and scorecard.BENCHMARKS == {'KRW': '069500', 'USD': 'SPY'}


def test_a_position_sold_in_pieces_is_one_round_trip_until_nothing_is_left():
    closed, still = round_trips([fill(1, '005930', 'BUY', 10, 100.0), fill(2, '005930', 'SELL', 6, 110.0, realized=59.0),
                                 fill(3, '005930', 'SELL', 4, 120.0, realized=79.0)])
    assert len(closed) == 1 and closed[0]['pnl'] == 138.0 and still == []


# ---- criteria version 2: drawdown against the index ------------------------------------------------------------------------------

def test_the_drawdown_bar_follows_the_index_with_a_floor():
    assert judge(drawdown=12.0, index_dd=15.0)['status'] == 'pass'                         # the market fell harder: still fine
    assert judge(drawdown=2.9, index_dd=1.0)['status'] == 'pass'                           # a calm market: the 3% floor
    assert judge(drawdown=3.1, index_dd=1.0)['status'] == 'fail'
    check = next(c for c in judge(drawdown=12.0, index_dd=15.0)['checks'] if c['key'] == 'drawdown_KRW')
    assert '지수 15.00%' in check['detail'] and '허용 15.00%' in check['detail']


def test_the_drawdown_waits_for_the_index_bars():
    verdict = judge(index_dd=None)
    assert next(c for c in verdict['checks'] if c['key'] == 'drawdown_KRW')['ok'] is None and verdict['status'] == 'collecting'


def test_a_plan_fixed_under_the_old_criteria_keeps_its_5_percent_rule():
    old = {**verification.start(CFG, NOW-60*DAY)}
    old['criteria'] = {'min_days': 56, 'min_trades': 30, 'min_decisions': 100, 'decision_horizon': 'd5', 'max_drawdown_pct': 5.0,
                       'drop_best_days': 3}
    assert judge(plan=old, drawdown=5.5, index_dd=20.0)['status'] == 'fail'
    assert judge(plan=old, drawdown=4.0, index_dd=1.0)['status'] == 'pass'
    assert verification.CRITERIA['criteria_version'] == 2 and 'max_drawdown_pct' not in verification.CRITERIA


# ---- the official verdict ----------------------------------------------------------------------------------------------------------

def test_the_first_decided_verdict_is_frozen_and_later_ones_do_not_replace_it():
    assert verification.official(judge(days=30), NOW) is None                              # still collecting
    first = verification.official(judge(drawdown=9.0), NOW)
    assert first['status'] == 'fail' and first['time'] == NOW
    plan = verification.start(CFG, NOW-60*DAY)
    plan['official'] = first
    later = judge(plan=plan)                                                                # a lucky pass afterwards
    assert later['status'] == 'pass' and later['official']['status'] == 'fail'
    assert verification.official(later, NOW) is None                                       # never re-frozen


def test_the_engine_freezes_it_once(tmp_path, monkeypatch):
    from app.engine import Engine
    from app.store import Store
    config = Config(database_url='sqlite:///'+str(tmp_path/'v.db'), mode='demo', password='x'*8, session_secret='y'*32)
    store = Store(config.database_url, config.mode)
    engine = Engine(config, store)
    engine.boot()
    engine.new_experiment(1000000, 1000, 'v', strategy_mode='intraday', strategy_settings={'horizon': 'month'})
    decided = {'status': 'pass', 'progress': [], 'checks': [], 'strategy_changed': False}
    monkeypatch.setattr(verification, 'judge', lambda *a, **k: dict(decided, official=store.read()['verification'].get('official')))
    engine.lock_verdict(now=NOW)
    assert store.read()['verification']['official']['status'] == 'pass'
    decided['status'] = 'fail'
    engine.lock_verdict(now=NOW+1)
    assert store.read()['verification']['official'] == {'status': 'pass', 'time': NOW, 'strategy_changed': False, 'progress': [], 'checks': []}
    store.release()


# ---- the index statistics are cumulative (2026-10-03 review) -------------------------------------------------------------------

from app.scorecard import benchmark_entry as _bench


def _bars(points):
    return [{'time': t*DAY, 'close': c} for t, c in points]   # day t, stamped at its midnight like the provider does


def test_an_empty_answer_or_a_shorter_window_keeps_the_index_statistics():
    full = _bench('SPY', _bars([(0, 100), (1, 150), (2, 100), (3, 125)]), DAY-1)
    assert (full['return_pct'], full['max_drawdown_pct']) == (25.0, 33.333)
    assert {k: _bench('SPY', [], DAY-1, full)[k] for k in ('return_pct', 'max_drawdown_pct')} == {'return_pct': 25.0, 'max_drawdown_pct': 33.333}
    later = _bench('SPY', _bars([(3, 125), (4, 140)]), DAY-1, full)                     # the early peak is out of the window
    assert later['max_drawdown_pct'] == 33.333 and later['return_pct'] == 40.0 and later['peak'] == 150
    deeper = _bench('SPY', _bars([(4, 140), (5, 90)]), DAY-1, later)
    assert deeper['max_drawdown_pct'] == 40.0                                         # from the remembered 150


def test_an_entry_saved_before_the_peak_was_kept_is_upgraded():
    old = {'symbol': 'SPY', 'start_time': 0, 'start_close': 100, 'last_time': 3*DAY, 'last_close': 125, 'return_pct': 25.0,
           'max_drawdown_pct': 33.333}
    up = _bench('SPY', _bars([(1, 150), (2, 100), (3, 125), (4, 160)]), DAY-1, old)
    assert up['peak'] == 160 and up['max_drawdown_pct'] == 33.333 and up['return_pct'] == 60.0


def test_a_start_day_bar_that_arrives_late_becomes_the_start():
    """2026-10-06: the experiment started at 16:05 Seoul, after the 15:30 close, but the day's bar is marked complete only at
    midnight, so the index was first fixed at the previous session's close (10-02) and would have counted 10-06 twice."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    seoul = ZoneInfo('Asia/Seoul')
    midnight = lambda d: datetime(2026, 10, d, tzinfo=seoul).timestamp()
    started = datetime(2026, 10, 6, 16, 5, tzinfo=seoul).timestamp()
    first = benchmark_entry('069500', [{'time': midnight(1), 'close': 100}, {'time': midnight(2), 'close': 101}], started)
    assert first['start_close'] == 101
    rows = [{'time': midnight(2), 'close': 101}, {'time': midnight(6), 'close': 104}, {'time': midnight(7), 'close': 106}]
    later = benchmark_entry('069500', rows, started, first)
    assert later['start_close'] == 104 and later['start_time'] == midnight(6) and later['return_pct'] == pytest.approx(106/104*100-100, abs=1e-3)
    assert benchmark_entry('069500', rows, started, later)['start_close'] == 104                # and then it stays
    early = datetime(2026, 10, 6, 11, 0, tzinfo=seoul).timestamp()                             # mid-session start
    assert benchmark_entry('069500', rows, early, first)['start_close'] == 101


def test_the_index_collects_its_dividends_like_the_account_does():
    """The account is paid its dividends after withholding, so the index it is compared with is too (2026-10-06)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ny = ZoneInfo('America/New_York')
    day = lambda m, d: datetime(2026, m, d, tzinfo=ny).timestamp()
    started = datetime(2026, 10, 6, 3, 5, tzinfo=ny).timestamp()
    divs = [{'ex_date': '2026-09-18', 'amount': 1.9}, {'ex_date': '2026-12-18', 'amount': 2.0}, {'ex_date': '2026-10-02', 'amount': 'x'}]
    first = benchmark_entry('SPY', [{'time': day(10, 5), 'close': 100.0}], started, None, divs, 0.15)
    assert first['dividend_per_share'] == 0 and first['return_pct'] == 0             # September's was before the start
    rows = [{'time': day(10, 5), 'close': 100.0}, {'time': day(12, 18), 'close': 99.0}]
    later = benchmark_entry('SPY', rows, started, first, divs, 0.15)
    assert later['dividends'] == {'2026-12-18': 1.7} and later['price_return_pct'] == -1.0
    assert later['return_pct'] == pytest.approx(0.7, abs=1e-3) and later['max_drawdown_pct'] == 1.0
    kept = benchmark_entry('SPY', rows+[{'time': day(12, 21), 'close': 99.5}], started, later, None, 0.15)   # a stale pack
    assert kept['dividends'] == {'2026-12-18': 1.7} and kept['return_pct'] == pytest.approx(1.2, abs=1e-3)


def test_the_index_checks_wait_for_a_close_after_the_start():
    """A fresh experiment showed both index comparisons as passed on day 0 (0.00% against 0.00%)."""
    flat = {'name': 'KODEX 200', 'start_time': NOW-DAY, 'last_time': NOW-DAY, 'return_pct': 0.0, 'max_drawdown_pct': 0.0}

    def checks(entry):
        state = {'verification': verification.start(CFG, NOW), 'initial': {'KRW': 1_000_000, 'USD': 0},
                 'performance': {'KRW': {'return_pct': 0.0, 'max_drawdown_pct': 0.0}}, 'benchmark': {'KRW': entry}}
        verdict = verification.judge(state, tracking={}, report={'closed': 0, 'expectancy_pct': None, 'ci_pct': None, 'groups': {}},
                                     evaluation={}, config=CFG, now=NOW+60)
        return {c['key']: c for c in verdict['checks']}
    day0 = checks(flat)
    assert day0['benchmark_KRW']['ok'] is None and day0['drawdown_KRW']['ok'] is None
    assert '다음 종가를 기다립니다' in day0['benchmark_KRW']['detail']
    later = checks(dict(flat, last_time=NOW+DAY, return_pct=1.0))
    assert later['benchmark_KRW']['ok'] is False and later['drawdown_KRW']['ok'] is True
