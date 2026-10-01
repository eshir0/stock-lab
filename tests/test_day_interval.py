"""How often the desk looks: a month plan every 20 minutes, day trading every 10 minutes and every 5 while it holds
something. Throw-away ledger, synthetic quotes, scripted answers; no market data or AI service is contacted."""
import time

import pytest

from app.config import Config
from app.engine import Engine
from app.store import Store
from test_month_desk import MonthProvider, PASSWORD, SECRET, open_position, run_cycle

DAY_SETTINGS = {'horizon': 'intraday', 'universe_mode': 'fixed', 'include_leveraged_etfs': False}


def make(tmp_path, settings, db='interval.db', **config):
    cfg = Config(database_url='sqlite:///'+str(tmp_path/db), mode='demo', password=PASSWORD, session_secret=SECRET,
                 toss_id='', toss_secret='', gemini_key='', fee_kr=15, fee_us=15, sell_tax_kr=0, slippage_bps=5, **config)
    store = Store(cfg.database_url, cfg.mode)
    engine = Engine(cfg, store, MonthProvider())
    engine.boot()
    engine.new_experiment(1000000, 1000, 'interval test', strategy_mode='intraday', strategy_settings=settings)
    engine.set_execution('auto')
    engine.refresh()
    engine.start()
    return engine, store


@pytest.fixture
def day(tmp_path):
    engine, store = make(tmp_path, DAY_SETTINGS)
    yield engine
    store.release()


def test_the_defaults_are_twenty_minutes_for_a_month_plan_and_ten_or_five_for_day_trading():
    c = Config()
    assert (c.interval_seconds, c.daytrade_interval_seconds, c.daytrade_active_interval_seconds) == (1200, 600, 300)


def test_the_interval_follows_the_investment_horizon(tmp_path):
    month, store = make(tmp_path, {**DAY_SETTINGS, 'horizon': 'month'})
    assert month.interval_plan(month.store.read()) == (1200, None) and month.analysis_interval(month.store.read()) == 1200
    store.release()


def test_day_trading_looks_every_ten_minutes_and_every_five_while_holding(day):
    state = day.store.read()
    assert day.interval_plan(state) == (600, 300) and day.analysis_interval(state) == 600
    open_position(day)
    assert day.analysis_interval(day.store.read()) == 300


def test_a_waiting_proposal_also_counts_as_busy(day):
    state = day.store.read()
    state['proposals'].append({'id': 'p', 'status': 'pending', 'symbol': '005930'})
    assert day.analysis_interval(state) == 300
    state['proposals'][0]['status'] = 'expired'
    assert day.analysis_interval(state) == 600


def test_the_faster_interval_is_never_slower_than_the_usual_one(tmp_path):
    engine, store = make(tmp_path, DAY_SETTINGS, daytrade_interval_seconds=240, daytrade_active_interval_seconds=900)
    open_position(engine)
    assert engine.interval_plan(engine.store.read()) == (240, 240) and engine.analysis_interval(engine.store.read()) == 240
    store.release()


def test_a_fixed_five_minutes_is_one_setting_away(tmp_path):
    engine, store = make(tmp_path, DAY_SETTINGS, daytrade_interval_seconds=300, daytrade_active_interval_seconds=300)
    assert engine.analysis_interval(engine.store.read()) == 300
    store.release()


def test_the_basic_strategy_keeps_the_general_interval(tmp_path):
    cfg = Config(database_url='sqlite:///'+str(tmp_path/'basic.db'), mode='demo', password=PASSWORD, session_secret=SECRET, toss_id='', toss_secret='', gemini_key='')
    store = Store(cfg.database_url, cfg.mode)
    engine = Engine(cfg, store, MonthProvider())
    engine.boot()
    engine.new_experiment(1000000, 1000, 'basic', strategy_mode='legacy')
    assert engine.analysis_interval(engine.store.read()) == 1200
    store.release()


def test_the_next_analysis_is_scheduled_at_the_start_of_a_cycle_with_the_interval_that_fits_the_moment(day):
    before = time.time()
    state = run_cycle(day)                                   # flat when it starts -> the usual 10 minutes
    assert state['runs'][-1]['status'] == 'completed' and state['positions']       # the scripted answer bought
    assert 595 <= state['next_run']-before <= 615
    before = time.time()
    state = run_cycle(day)                                   # holding when it starts -> the quick 5 minutes
    assert 295 <= state['next_run']-before <= 315


def test_the_dashboard_is_told_both_numbers_for_day_trading_and_one_for_a_month_plan(tmp_path):
    engine, store = make(tmp_path, DAY_SETTINGS)
    config = engine.public_state()['config']
    assert (config['interval'], config['interval_active']) == (600, 300)
    store.release()
    month, store = make(tmp_path, {**DAY_SETTINGS, 'horizon': 'month'}, db='month.db')
    config = month.public_state()['config']
    assert (config['interval'], config['interval_active']) == (1200, None)
    store.release()
