"""Quota pacing: a window that cannot pay for an analysis every 10 minutes gets them spread over the session instead of used up
at once. Scripted answers and fake usage readings; no network, no real AI."""
import time

import pytest

from app import pacing
from app.config import Config
from app.engine import Engine
from app.store import Store
from test_month_desk import MonthProvider, PASSWORD, SECRET, run_cycle
from test_usage_gate import HOUR, reading

NOW = 1_790_700_000.0


# ---- the rule --------------------------------------------------------------------------------------------------------------

def pace(pct=0.0, cost=20.0, hours=5.0, until_hours=6.5, base=600, switch=80.0):
    return pacing.pace(base, pct=pct, switch_pct=switch, resets_at=NOW+hours*HOUR, until=NOW+until_hours*HOUR, now=NOW, cost=cost)


def test_a_fresh_window_pays_for_four_analyses_spread_over_five_hours_not_ten_minutes_apart():
    assert pace(pct=0, cost=20, hours=5) == 75*60                        # 4 analyses fit: the running one and 3 more, 75 minutes apart


def test_the_pause_is_the_time_left_divided_by_the_analyses_still_affordable():
    assert pace(pct=20, cost=20, hours=2) == 2400                        # after this one 40 points remain: 2 more, so 3 slots in 2 hours


def test_the_session_end_counts_when_it_comes_before_the_window_reset():
    assert pace(pct=0, cost=20, hours=5, until_hours=1.0) == 900         # the running analysis and 3 more share the hour: 15 minutes apart


def test_a_window_with_plenty_of_room_keeps_the_configured_interval():
    assert pace(pct=0, cost=3, hours=1) == 600                           # cheap analyses: 600 s is already slower than needed


def test_the_interval_is_never_shorter_than_the_configured_minimum_or_longer_than_the_cap():
    assert pace(pct=0, cost=20, hours=50, until_hours=50) == pacing.PACE_MAX
    assert pace(pct=0, cost=1, hours=0.1, until_hours=0.1, base=300) == 300


@pytest.mark.parametrize('case', [dict(pct=70), dict(pct=61, cost=20), dict(pct=80), dict(hours=0), dict(until_hours=0), dict(hours=-1)])
def test_when_nothing_more_is_affordable_or_no_time_is_left_the_base_interval_stays(case):
    assert pace(**case) == 600


def test_missing_readings_mean_no_pacing():
    assert pacing.pace(600, pct=None, switch_pct=80, resets_at=NOW+HOUR, until=NOW+HOUR, now=NOW, cost=15) == 600
    assert pacing.pace(600, pct=10, switch_pct=80, resets_at=None, until=NOW+HOUR, now=NOW, cost=15) == 600
    assert pacing.pace(600, pct=10, switch_pct=80, resets_at=NOW+HOUR, until=None, now=NOW, cost=15) == 600


def test_the_cost_estimate_is_the_median_of_recent_readings_and_skips_unusable_ones():
    assert pacing.learn(None, 2.0, 21.0) == ([19.0], 19.0)
    assert pacing.learn(None, None, 30.0) == ([], pacing.COST_DEFAULT)
    assert pacing.learn([10.0], 50.0, 10.0) == ([10.0], 10.0)                       # the window reset in between: skipped
    assert pacing.learn([10.0], 10.0, 10.0) == ([10.0], 10.0)
    assert pacing.learn([], 0.0, 99.0)[1] == pacing.COST_MAX and pacing.learn([], 10.0, 10.1)[1] == pacing.COST_MIN
    # other use of the same subscription while an analysis ran inflates one reading: the median shrugs it off
    samples, cost = pacing.learn([10.0, 11.0, 9.0, 10.0], 20.0, 60.0)
    assert samples[-1] == 40.0 and cost == 10.0
    samples, cost = pacing.learn([30.0]*20, 0.0, 5.0)                             # only the last SAMPLES readings count
    assert len(samples) == pacing.SAMPLES and cost == 30.0
    assert pacing.learn([True, 'x', 12.0], None, None) == ([12.0], 12.0)
    assert pacing.learn([10.0, 20.0], None, None) == ([10.0, 20.0], 15.0)


# ---- in the desk -----------------------------------------------------------------------------------------------------------

def make(tmp_path, pacing_on=True, horizon='month', **over):
    config = Config(database_url='sqlite:///'+str(tmp_path/'pace.db'), mode='demo', password=PASSWORD, session_secret=SECRET, toss_id='',
                    toss_secret='', gemini_key='', fee_kr=15, fee_us=15, sell_tax_kr=0, slippage_bps=5, quota_pacing=pacing_on,
                    bridge_url='http://bridge.invalid', bridge_token='t'*40, providers='claude,codex', ai_switch_pct=80.0, **over)
    store = Store(config.database_url, config.mode)
    engine = Engine(config, store, MonthProvider())
    engine.agents.gate.enabled = True                      # demo answers are scripted, but the rule under test reads the gate
    engine.boot()
    engine.new_experiment(1000000, 1000, 'pace', strategy_mode='intraday',
                          strategy_settings={'horizon': horizon, 'universe_mode': 'fixed', 'include_leveraged_etfs': False})
    engine.set_execution('auto')
    engine.refresh()
    engine.readings = {}
    engine.agents.gate.fetch = lambda: engine.readings
    engine.signal_gate = False                             # every cycle analyses: the interval, not the gate, is under test
    engine.start()
    return engine, store


def set_usage(engine, five, resets_in_hours):
    now = time.time()
    engine.readings = {'claude': {'limits': reading(five, .10, now=now, five_reset=now+resets_in_hours*HOUR)}, 'codex': {'cooldown_until': now+4*86400}}
    engine.agents.gate.cache = None


@pytest.fixture
def desk(tmp_path):
    engine, store = make(tmp_path)
    yield engine
    store.release()


def gap(state, started):
    return state['next_run']-started


def test_the_desk_schedules_the_next_analysis_later_when_the_window_is_tight(desk):
    set_usage(desk, .20, 2)                                              # 20% used, 2 hours to the reset, about 15% an analysis
    started = time.time()
    state = run_cycle(desk)
    info = state['pacing']
    assert info['paced'] and info['base'] == 1200 and info['pct'] == 20.0 and info['provider'] == 'claude'
    assert 1200 < gap(state, started) < 4500 and gap(state, started) == pytest.approx(info['interval'], abs=3)
    expected = pacing.pace(1200, pct=20.0, switch_pct=80.0, resets_at=info['resets_at'], until=info['until'], now=started, cost=pacing.COST_DEFAULT)
    assert info['interval'] == pytest.approx(expected, abs=5)


def test_with_room_to_spare_the_interval_stays_twenty_minutes(desk):
    set_usage(desk, .0, 0.3)                                             # the window resets in 18 minutes
    started = time.time()
    state = run_cycle(desk)
    assert not state['pacing']['paced'] and 1195 <= gap(state, started) <= 1215


def test_a_held_position_no_longer_shortens_the_interval(desk):
    """The 5-minute 'busy' interval belonged to day trading (removed 2026-10-07): a month position is watched by the exit
    monitor every poll, and the analysis interval stays the same."""
    set_usage(desk, .0, 0.2)
    state = run_cycle(desk)                                              # the scripted answer buys
    assert state['positions']
    started = time.time()
    state = run_cycle(desk)
    assert state['pacing']['base'] == 1200 and 1195 <= gap(state, started) <= 1215


def test_pacing_can_be_switched_off(tmp_path):
    engine, store = make(tmp_path, pacing_on=False)
    set_usage(engine, .20, 2)
    started = time.time()
    state = run_cycle(engine)
    assert not state['pacing']['paced'] and 1195 <= gap(state, started) <= 1215
    store.release()


def test_an_ai_without_a_usage_window_is_not_paced(desk):
    desk.readings = {'claude': {'cooldown_until': time.time()+86400}, 'codex': {}}          # only Codex is usable, and it reports no window
    desk.agents.gate.cache = None
    started = time.time()
    state = run_cycle(desk)
    assert not state['pacing']['paced'] and state['pacing']['pct'] is None and 1195 <= gap(state, started) <= 1215


def test_a_month_plan_is_paced_the_same_way(tmp_path):
    engine, store = make(tmp_path, horizon='month')
    set_usage(engine, .20, 2)
    engine.signal_gate = False
    started = time.time()
    state = run_cycle(engine)
    info = state['pacing']
    expected = pacing.pace(1200, pct=20.0, switch_pct=80.0, resets_at=info['resets_at'], until=info['until'], now=started,
                           cost=pacing.COST_DEFAULT)
    assert info['paced'] and expected > 1200 and abs(gap(state, started)-expected) <= 15
    store.release()


def test_the_desk_learns_what_an_analysis_costs_from_the_window_reading(desk):
    set_usage(desk, .10, 4)
    calls = []
    real = desk.agents.run

    def spy(role, context, generation):
        calls.append(role)
        if len(calls) == 3:                                              # the bridge now reports the window at 40% after this call
            now = time.time()
            desk.agents.gate.observe('claude', limits=reading(.40, .10, now=now, five_reset=now+4*HOUR))
        return real(role, context, generation)
    desk.agents.run = spy
    run_cycle(desk)
    tracked = desk.store.read()['pacing']
    assert tracked['samples'] == [30.0] and tracked['cost_pct'] == 30.0


def test_a_window_that_resets_during_an_analysis_is_not_counted_as_cost(desk):
    set_usage(desk, .70, 1)
    calls = []
    real = desk.agents.run

    def spy(role, context, generation):
        calls.append(role)
        if len(calls) == 2:                                              # a new window: usage falls back to 2%
            now = time.time()
            desk.agents.gate.observe('claude', limits=reading(.02, .10, now=now, five_reset=now+5*HOUR))
        return real(role, context, generation)
    desk.agents.run = spy
    run_cycle(desk)
    assert desk.store.read()['pacing']['cost_pct'] == pacing.COST_DEFAULT


def test_the_dashboard_state_carries_the_pacing_numbers(desk):
    set_usage(desk, .20, 2)
    run_cycle(desk)
    public = desk.public_state()
    assert public['pacing']['paced'] and public['pacing']['interval'] > public['pacing']['base'] == 1200
    assert public['config']['interval'] == 1200 and public['config']['interval_active'] is None


def test_the_defaults_are_on_and_capped_at_seventy_five_minutes():
    c = Config()
    assert c.quota_pacing is True and c.pace_max_seconds == 4500


def test_a_backup_ai_with_room_keeps_the_usual_interval(desk):
    """2026-10-08: Claude at 21% stretched the interval to 59 minutes while Codex sat at 0%. At the switch level the next AI
    takes over, so with a backup that has room the analysis keeps its 20 minutes."""
    now = time.time()
    desk.readings = {'claude': {'limits': reading(.20, .10, now=now, five_reset=now+2*HOUR)},
                     'codex': {'limits': reading(.0, .09, now=now, five_reset=now+4*HOUR)}}
    desk.agents.gate.cache = None
    started = time.time()
    state = run_cycle(desk)
    assert not state['pacing']['paced'] and state['pacing']['backup'] == 'codex' and 1195 <= gap(state, started) <= 1215
    desk.readings['codex'] = {'limits': reading(.60, .09, now=now, five_reset=now+4*HOUR)}           # the backup is busy too
    desk.agents.gate.cache = None
    with desk.store.edit() as s:
        s['next_run'] = 0
    started = time.time()
    state = run_cycle(desk)
    assert state['pacing']['paced'] and gap(state, started) > 1215
