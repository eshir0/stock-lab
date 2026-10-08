"""Conditional entries on a throw-away ledger: a HOLD that carries a price plan is kept, watched on every poll without any AI call
and traded through the ordinary sizing and risk rules. Every way a plan can end is covered, and so is the memo of the previous
analysis. Synthetic quotes and scripted answers; no market data or AI service is contacted."""
import copy
import inspect
import time

import pytest

from app import entry
from app.config import Config
from app.desk import DeskMixin
from app.engine import Engine
from app.main import create_app
from app.providers import ProviderError
from app.store import Store
from test_month_desk import MonthProvider, PASSWORD, SECRET, open_position

DAY = {'horizon': 'intraday', 'universe_mode': 'fixed', 'include_leveraged_etfs': False}
MONTH = {'horizon': 'month', 'universe_mode': 'fixed', 'include_leveraged_etfs': False}
PRICE, LEVEL, DEAD = 70000.0, 70700.0, 69300.0


class EntryProvider(MonthProvider):
    def __init__(self):
        super().__init__()
        self.recent_volume = None          # volume of the last five 1-minute candles; None keeps the demo series (volume rising)

    def candles(self, symbol, interval='1d', count=None):
        rows = super().candles(symbol, interval, count)
        if interval == '1m' and self.recent_volume is not None:
            rows = [dict(r, volume=100_000) for r in rows]
            for row in rows[-5:]:
                row['volume'] = self.recent_volume
        return rows


def build(tmp_path, settings=DAY, execution='auto', **config):
    values = dict(fee_kr=15, fee_us=15, sell_tax_kr=0, slippage_bps=5)
    values.update(config)
    cfg = Config(database_url='sqlite:///'+str(tmp_path/'entry.db'), mode='demo', password=PASSWORD, session_secret=SECRET,
                 toss_id='', toss_secret='', gemini_key='', **values)
    store = Store(cfg.database_url, cfg.mode)
    engine = Engine(cfg, store, EntryProvider())
    engine.boot()
    engine.new_experiment(1000000, 1000, 'entry test', strategy_mode='intraday', strategy_settings=settings)
    engine.set_execution(execution)
    engine.provider.prices['005930'] = PRICE
    engine.refresh()
    engine.calls, engine.contexts, engine.script = [], [], {}
    real = engine.agents.run

    def run(role, context, generation):
        engine.calls.append(role)
        engine.contexts.append((role, copy.deepcopy(context)))
        report = real(role, context, generation)
        if role == 'director' and engine.script:
            report.update(engine.script)          # the scripted answer: what the director "decided"
        return report
    engine.agents.run = run
    engine.start()
    return engine, store


@pytest.fixture
def day(tmp_path):
    engine, store = build(tmp_path)
    yield engine
    store.release()


@pytest.fixture
def manual(tmp_path):
    engine, store = build(tmp_path, execution='manual')
    yield engine
    store.release()


@pytest.fixture
def month(tmp_path):
    engine, store = build(tmp_path, MONTH)
    yield engine
    store.release()


def plan_script(kind='breakout', level=LEVEL, invalidate=DEAD, minutes=60, **over):
    return {'stance': 'HOLD', 'quantity': 0, 'target_weight_pct': 20, 'stop_loss_pct': 2, 'take_profit_pct': 4,
            'max_holding_minutes': 60, 'entry_type': kind, 'entry_level': level, 'entry_invalidate': invalidate,
            'entry_minutes': minutes, **over}


def analyse(engine, symbol='005930'):
    engine.request_cycle(symbol)
    engine.cycle()
    return engine.store.read()


def poll(engine, times=1):
    """What the monitor does every ten seconds."""
    for _ in range(times):
        engine.refresh()
        engine.process_entry_watches()
    return engine.store.read()


def watched(engine, **script):
    engine.script = plan_script(**script)
    state = analyse(engine)
    engine.script = {}
    return state


def at(engine, price, symbol='005930'):
    engine.provider.prices[symbol] = price


# ---- the plan is kept from a HOLD ---------------------------------------------------------------------------------------

def test_a_hold_with_a_plan_becomes_a_waiting_watch_and_trades_nothing(day):
    state = watched(day)
    [watch] = state['watches']
    assert watch['status'] == 'waiting' and watch['symbol'] == '005930' and watch['type'] == 'breakout' and watch['horizon'] == 'intraday'
    assert watch['level'] == LEVEL and watch['invalidate'] == DEAD and watch['market'] == 'KR' and watch['currency'] == 'KRW'
    assert watch['plan'] == {'target_weight_pct': 20, 'stop_loss_pct': 2, 'take_profit_pct': 4, 'max_holding_minutes': 60}
    run = state['runs'][-1]
    assert watch['run_id'] == run['id'] and run['watch'] == {'status': 'waiting', 'id': watch['id'], 'note': entry.describe(watch)}
    assert state['trades'] == [] and state['positions'] == {} and state['cash']['KRW'] == 1000000
    messages = [e['message'] for e in state['events']]
    assert any('조건 진입 등록' in m and '70,700원' in m for m in messages) and any('관망' in m for m in messages)
    assert state['evaluations'][-1]['stance'] == 'HOLD'
    assert 59*60 <= watch['expires']-watch['created'] <= 60*60


def test_a_plan_the_server_declines_is_recorded_with_its_reason(day):
    state = watched(day, level=80000.0)                        # 14% above the price: not a plan for a single session
    assert state['watches'] == [] and state['runs'][-1]['watch']['status'] == 'rejected'
    assert '떨어져' in state['runs'][-1]['watch']['note']
    assert any('떨어져' in e['message'] for e in state['events'])


def test_a_name_that_is_already_held_gets_no_plan(day):
    open_position(day, '005930', price=PRICE, quantity=1)
    state = watched(day)
    assert state['watches'] == []
    assert state['runs'][-1]['watch'] == {'status': 'rejected', 'note': '이미 보유 중인 종목이라 조건 진입을 만들지 않았습니다.'}


def test_plans_of_another_market_do_not_fill_the_places(day):
    """Until 2026-10-07 three plans in all were the limit; now each market has its own six (test_entry_limit.py)."""
    now = time.time()
    with day.store.edit() as s:
        for symbol in ('000660', 'AAPL', 'MSFT'):
            fields = {'type': 'breakout', 'level': 1e9, 'invalidate': 1.0, 'expires': now+3600, 'plan': {}}
            s['watches'].append(entry.make_watch(fields, symbol=symbol, name=symbol, market='US', currency='USD', horizon='intraday',
                                                 reference=1.0, summary='', engine='', run_id='x', generation=1, now=now))
    state = watched(day)
    assert len(entry.waiting(state)) == 4 and any(w['symbol'] == '005930' for w in entry.waiting(state))


def test_a_newer_analysis_of_the_same_name_replaces_the_plan(day):
    watched(day)
    state = watched(day, level=70500.0, invalidate=69500.0, minutes=30)
    old, new = state['watches']
    assert old['status'] == 'replaced' and old['closed'] and new['status'] == 'waiting' and new['level'] == 70500.0
    assert len(entry.waiting(state)) == 1


def test_a_newer_analysis_without_a_plan_withdraws_the_old_one(day):
    watched(day)
    state = analyse(day)                                       # the scripted demo director now simply buys
    assert [w['status'] for w in state['watches']] == ['replaced'] and state['trades'] and state['trades'][-1]['side'] == 'BUY'
    assert not any('entry_watch' in t for t in state['trades'])


def test_a_watched_name_is_not_analysed_again_until_its_plan_ends(day):
    watched(day)
    with day.store.edit() as s:
        s['next_run'], s['last_market'] = 0, 'US'              # the next market is Korea again
        s['cursor'] = [i['symbol'] for i in day.active_instruments(s)].index('005930')
    day.calls.clear()
    day.cycle()
    state = day.store.read()
    assert state['runs'][-1]['symbol'] == '000660' and [r['symbol'] for r in state['runs']].count('005930') == 1
    assert entry.waiting(state)                                # and the plan is still waiting


def test_when_every_name_waits_on_a_plan_the_cycle_makes_no_ai_call(day):
    now = time.time()
    with day.store.edit() as s:
        for symbol in [i['symbol'] for i in day.active_instruments(s)]:
            fields = {'type': 'breakout', 'level': 1e9, 'invalidate': 1.0, 'expires': now+3600, 'plan': {}}
            s['watches'].append(entry.make_watch(fields, symbol=symbol, name=symbol, market='US', currency='USD', horizon='intraday',
                                                 reference=1.0, summary='', engine='', run_id='x', generation=1, now=now))
        s['next_run'] = 0
    day.calls.clear()
    day.cycle()
    state = day.store.read()
    assert day.calls == [] and state['runs'] == [] and '다시 분석하지 않습니다' in state['scheduler_status']


def test_conditional_entry_is_on_by_default(day):
    assert Config().conditional_entry is True and day.public_state()['config']['conditional_entry'] is True


def test_conditional_entry_can_be_switched_off(tmp_path):
    engine, store = build(tmp_path, conditional_entry=False)
    state = watched(engine)
    assert state['watches'] == [] and state['runs'][-1]['watch']['status'] == 'rejected'
    assert 'CONDITIONAL_ENTRY' in state['runs'][-1]['watch']['note'] and engine.public_state()['config']['conditional_entry'] is False
    assert state['trades'] == []
    store.release()


# ---- the price condition buys ---------------------------------------------------------------------------------------------

def test_the_price_condition_buys_through_the_ordinary_rules_after_two_confirming_polls_with_no_ai_call(day):
    watched(day)
    at(day, LEVEL)
    state = poll(day)                                          # the first fresh quote in the zone
    assert state['trades'] == [] and state['watches'][0]['hits'] == 1
    day.calls.clear()
    state = poll(day)                                          # the second one
    [watch] = state['watches']
    assert watch['status'] == 'filled' and watch['outcome'].endswith('자동 모의매수') and watch['closed']
    [trade] = state['trades']
    assert (trade['side'], trade['symbol'], trade['execution_mode']) == ('BUY', '005930', 'auto') and trade['entry_watch'] == watch['id']
    position = state['positions']['005930']
    assert position['quantity'] == trade['quantity'] and position['stop_price'] < LEVEL < position['take_profit_price']
    assert position['strategy_mode'] == 'intraday' and position['entry_thesis'].startswith('조건 진입(')
    assert day.calls == []                                     # not one AI call
    proposal = state['proposals'][-1]
    assert (proposal['origin'], proposal['status'], proposal['id'], proposal['watch_id']) == ('watch', 'filled', trade['reference'], watch['id'])
    record = state['evaluations'][-1]
    assert record['selected_by'] == 'watch' and record['stance'] == 'BUY' and record['engine'].endswith('조건 진입') and record['action'] == 'filled'
    assert any('조건 진입' in e['message'] and '자동 체결' in e['message'] for e in state['events'])
    assert state['shadow_orders'][-1]['source'] == 'watch'
    assert poll(day)['trades'] == state['trades']                # the plan is used up: no second order


def test_the_size_comes_from_the_same_sizing_as_an_analysed_buy(day):
    watched(day)
    at(day, LEVEL)
    poll(day, 2)
    state = day.store.read()
    trade = state['trades'][0]
    # 20% of a 1,000,000 won account at about 70,700 won is two whole shares: the plan's weight, not anything the watch invents
    assert trade['quantity'] == 2 and trade['sizing']['quantity'] == 2 and trade['sizing']['target_quantity'] == 2
    assert state['cash']['KRW'] < 1000000 - 2*LEVEL


def test_a_single_quote_in_the_zone_is_not_enough(day):
    watched(day)
    at(day, LEVEL)
    assert poll(day)['watches'][0]['hits'] == 1
    at(day, 70300.0)                                           # back below the level
    state = poll(day, 2)
    assert state['watches'][0]['hits'] == 0 and state['trades'] == [] and state['watches'][0]['status'] == 'waiting'


def test_a_quote_nobody_refreshed_is_not_counted_twice(day):
    watched(day)
    at(day, LEVEL)
    day.refresh()
    day.process_entry_watches()
    day.process_entry_watches()                                # the same quote again: still one look
    assert day.store.read()['watches'][0]['hits'] == 1


def test_a_breakout_that_has_already_run_is_not_chased_but_is_bought_when_it_comes_back(day):
    watched(day)
    at(day, 71200.0)                                           # 0.71% above the level, outside the 0.5% zone
    state = poll(day, 3)
    watch = state['watches'][0]
    assert state['trades'] == [] and watch['hits'] == 0 and '쫓아 사지 않고' in watch['note']
    at(day, 70800.0)
    state = poll(day, 2)
    assert state['watches'][0]['status'] == 'filled' and state['watches'][0]['note'] == ''


def test_a_breakout_on_thin_volume_waits_and_trades_once_the_volume_is_there(day):
    watched(day)
    day.provider.recent_volume = 50_000                        # half of the 100,000 a minute before
    at(day, LEVEL)
    state = poll(day, 2)
    watch = state['watches'][0]
    assert watch['status'] == 'waiting' and '거래량이 받쳐주지 않아' in watch['note'] and watch['volume_after'] > time.time()
    assert state['trades'] == []
    day.provider.recent_volume = 300_000
    assert poll(day)['trades'] == []                           # still inside the 30 s back-off
    with day.store.edit() as s:
        s['watches'][0]['volume_after'] = 0
    state = poll(day)
    assert state['watches'][0]['status'] == 'filled' and len(state['trades']) == 1


def test_a_breakout_needs_usable_candles_to_check_the_volume(day):
    watched(day)
    at(day, LEVEL)
    real = day.provider.candles
    day.provider.candles = lambda symbol, interval='1d', count=None: [] if interval == '1m' else real(symbol, interval, count)
    state = poll(day, 2)
    assert state['watches'][0]['status'] == 'waiting' and '1분봉' in state['watches'][0]['note'] and state['trades'] == []


def test_a_pullback_buys_when_the_price_falls_to_its_level_and_needs_no_volume(day):
    watched(day, kind='pullback', level=69600.0, invalidate=68900.0)
    day.provider.recent_volume = 1                             # no volume check for a pullback
    at(day, 69500.0)
    state = poll(day, 2)
    assert state['watches'][0]['status'] == 'filled' and state['trades'][0]['price'] < 69600*1.001


# ---- every other way a plan ends -----------------------------------------------------------------------------------------

def test_the_plan_is_dropped_when_the_price_breaks_the_invalidation_level(day):
    watched(day)
    at(day, 69200.0)
    state = poll(day)
    [watch] = state['watches']
    assert watch['status'] == 'invalid' and '무효 기준' in watch['outcome'] and state['trades'] == []
    assert any('폐기' in e['message'] for e in state['events'])
    at(day, LEVEL)
    assert poll(day, 2)['trades'] == []                        # and it does not come back to life


def test_a_plan_expires_when_its_time_is_up_even_with_the_market_closed(day):
    watched(day)
    day.provider.closed.add('005930')
    with day.store.edit() as s:
        s['watches'][0]['expires'] = time.time()-1
    state = poll(day)
    assert state['watches'][0]['status'] == 'expired' and '유효 시간' in state['watches'][0]['outcome']


def test_nothing_is_judged_while_the_market_is_closed(day):
    watched(day)
    day.provider.closed.add('005930')
    at(day, LEVEL)
    state = poll(day, 3)
    assert state['watches'][0]['hits'] == 0 and state['watches'][0]['status'] == 'waiting' and state['trades'] == []


def test_a_name_that_left_the_focus_list_is_cancelled(day, monkeypatch):
    watched(day)
    at(day, LEVEL)
    monkeypatch.setattr(day, 'focus_allows', lambda state, symbol: False)
    state = poll(day)
    assert state['watches'][0]['status'] == 'cancelled' and '집중 종목' in state['watches'][0]['outcome'] and state['trades'] == []


def test_a_plan_the_risk_rules_refuse_is_closed_with_the_reason(day):
    watched(day)
    with day.store.edit() as s:
        day.update_desk_risk(s)
        s['risk_days']['KRW']['halted'] = True                 # the day's loss limit was reached
    at(day, LEVEL)
    state = poll(day, 2)
    watch = state['watches'][0]
    assert watch['status'] == 'blocked' and '일일 손실 한도' in watch['outcome'] and state['trades'] == [] and state['positions'] == {}
    assert any(e['level'] == 'warning' and '위험 규칙에 막혀' in e['message'] for e in state['events'])


def test_a_plan_with_no_money_left_for_even_one_share_is_closed_not_retried_forever(day):
    watched(day)
    with day.store.edit() as s:
        s['cash']['KRW'] = 10000.0                            # less than one share
    at(day, LEVEL)
    state = poll(day, 2)
    assert state['watches'][0]['status'] == 'blocked' and state['watches'][0]['outcome'] and state['trades'] == []
    assert state['watches'][0]['failures'] == 0               # a refusal by the rules, not a bad moment


def test_a_refusal_that_may_pass_is_retried_a_few_times_then_dropped(day, monkeypatch):
    watched(day)
    at(day, LEVEL)

    def refuse(*args, **kwargs):
        raise ValueError('보유 종목의 최신 평가 시세가 부족합니다.')
    monkeypatch.setattr(day, 'place_desk_order', refuse)
    poll(day)                                                  # the first look in the zone
    for attempt in range(1, entry.MAX_FAILURES):
        state = poll(day)
        assert state['watches'][0]['status'] == 'waiting' and state['watches'][0]['failures'] == attempt
    state = poll(day)
    assert state['watches'][0]['status'] == 'blocked' and '시세가 부족' in state['watches'][0]['outcome']


def test_a_quote_problem_at_the_moment_of_the_order_just_waits(day, monkeypatch):
    watched(day)
    at(day, LEVEL)
    poll(day)

    def down(symbol):
        raise ProviderError('quote unavailable')
    monkeypatch.setattr(day, 'quote_for_trade', down)
    state = poll(day)                                          # the trigger cannot get a fresh quote
    assert state['watches'][0]['status'] == 'waiting' and state['watches'][0]['failures'] == 0 and state['trades'] == []
    monkeypatch.undo()
    assert poll(day)['watches'][0]['status'] == 'filled'


def test_a_price_that_leaves_the_zone_between_the_poll_and_the_order_resets_the_count(day, monkeypatch):
    watched(day)
    at(day, LEVEL)
    poll(day)
    real = day.quote_for_trade
    monkeypatch.setattr(day, 'quote_for_trade', lambda symbol: dict(real(symbol), ask=70300.0, bid=70300.0))
    state = poll(day)
    assert state['watches'][0]['hits'] == 0 and state['watches'][0]['status'] == 'waiting' and state['trades'] == []


@pytest.mark.parametrize('how', ['stop', 'liquidate'])
def test_stopping_or_liquidating_cancels_the_waiting_plans(day, how):
    watched(day)
    getattr(day, how)()
    [watch] = day.store.read()['watches']
    assert watch['status'] == 'cancelled' and watch['closed'] and watch['outcome']


def test_a_short_restart_keeps_the_waiting_plans_and_they_still_work(day):
    watched(day)
    day.shutdown()
    assert day.store.read()['watches'][0]['status'] == 'waiting'          # shutting down alone decides nothing
    day.boot()
    state = day.store.read()
    [watch] = state['watches']
    assert watch['status'] == 'waiting' and state['running'] and any('그대로 유지합니다' in e['message'] for e in state['events'])
    at(day, LEVEL)
    assert poll(day, 2)['watches'][0]['status'] == 'filled'


def test_a_restart_does_not_jump_the_queue_of_the_next_analysis(day):
    with day.store.edit() as s:
        s['next_run'] = time.time()+900                                   # the pacing scheduled it for later
    day.shutdown()
    day.boot()
    assert day.store.read()['next_run'] >= time.time()+890
    with day.store.edit() as s:
        s['next_run'] = time.time()-60                                    # it came due while the server was down
    day.shutdown()
    day.boot()
    assert time.time()-5 <= day.store.read()['next_run'] <= time.time()+1
    day.stop()
    day.boot()                                                            # a stopped desk has nothing scheduled
    assert not day.store.read()['running']
    day.start()                                                           # and 시작 starts at once, whatever was scheduled
    with day.store.edit() as s:
        s['next_run'] = time.time()+900
    day.stop()
    day.start()
    assert day.store.read()['next_run'] <= time.time()+1


def test_a_long_outage_drops_the_waiting_plans(day):
    watched(day)
    day.shutdown()
    with day.store.edit() as s:
        for quote in s['quotes'].values():
            quote['received'] -= entry.RESTART_GRACE+60                  # nothing was written for a while
    day.boot()
    [watch] = day.store.read()['watches']
    assert watch['status'] == 'cancelled' and '다시 시작' in watch['outcome']


def test_a_desk_the_owner_had_stopped_does_not_keep_plans_across_a_restart(day):
    watched(day)
    with day.store.edit() as s:
        s['resume'] = False                                              # e.g. a crash while the owner's stop was being saved
    day.shutdown()
    day.boot()
    assert day.store.read()['watches'][0]['status'] == 'cancelled'


def test_nothing_is_watched_while_stopped(day):
    watched(day)
    at(day, LEVEL)
    with day.store.edit() as s:
        s['running'] = False
    poll(day, 3)
    state = day.store.read()
    assert state['trades'] == [] and state['watches'][0]['hits'] == 0


def test_a_new_experiment_starts_without_plans(day):
    watched(day)
    day.stop()
    day.new_experiment(1000000, 1000, 'next', strategy_mode='intraday', strategy_settings=DAY)
    assert day.store.read()['watches'] == []


# ---- manual mode and the month horizon -------------------------------------------------------------------------------------

def test_in_manual_mode_a_triggered_plan_becomes_a_proposal_to_approve(manual):
    watched(manual)
    at(manual, LEVEL)
    state = poll(manual, 2)
    [watch] = state['watches']
    assert watch['status'] == 'proposed' and state['trades'] == [] and '승인 대기' in watch['outcome']
    [proposal] = [p for p in state['proposals'] if p['status'] == 'pending']
    assert proposal['origin'] == 'watch' and proposal['side'] == 'BUY' and proposal['watch_id'] == watch['id']
    assert manual.approve(proposal['id']) == {'status': 'filled'}
    state = manual.store.read()
    assert state['trades'][0]['entry_watch'] == watch['id'] and state['positions']['005930']['quantity'] == proposal['quantity']


def test_a_month_plan_waits_for_hours_and_opens_a_month_position(month):
    month.script = plan_script(stop_loss_pct=5, take_profit_pct=10, max_holding_minutes=20160, minutes=600)
    state = analyse(month)
    month.script = {}
    [watch] = state['watches']
    assert watch['horizon'] == 'month' and 599*60 <= watch['expires']-watch['created'] <= 600*60
    at(month, LEVEL)
    state = poll(month, 2)
    assert state['watches'][0]['status'] == 'filled'
    position = state['positions']['005930']
    assert position['horizon'] == 'month' and position['trail_pct'] == 5 and position['initial_stop'] == position['stop_price']
    assert state['evaluations'][-1]['horizon'] == 'month' and state['evaluations'][-1]['selected_by'] == 'watch'


# ---- the memo of the previous analysis -------------------------------------------------------------------------------------

def test_the_next_analysis_of_a_name_starts_from_a_memo_of_the_last_one(day):
    watched(day)
    analyse(day)
    first, second = [c for role, c in day.contexts if role == 'planner']
    assert 'previous' not in first
    previous = second['previous']
    assert previous['stance'] == 'HOLD' and previous['minutes_ago'] == 0 and previous['price_then'] == PRICE
    assert previous['price_change_pct'] == 0.0 and previous['summary']
    assert previous['entry_plan'] == {'type': '돌파 매수', 'level': LEVEL, 'invalidate': DEAD, 'result': '대기 중'}
    assert [c for role, c in day.contexts if role == 'director'][1]['previous'] == previous


def test_the_memo_reports_how_the_plan_ended_and_how_far_the_price_moved(day):
    watched(day)
    at(day, 69200.0)
    poll(day)                                                  # the plan dies
    at(day, 71400.0)
    day.refresh()                                              # the poll sees the new price before the next analysis
    analyse(day)
    previous = [c for role, c in day.contexts if role == 'planner'][1]['previous']
    assert previous['entry_plan']['result'] == '무효 가격 이탈' and previous['price_change_pct'] == 2.0


def test_the_memo_is_left_out_when_there_is_nothing_to_remember():
    assert DeskMixin.previous_note({'runs': [], 'evaluations': []}, '005930', time.time(), {'bid': 1, 'ask': 1}) is None
    running = {'runs': [{'symbol': '005930', 'status': 'running', 'reports': [], 'id': 'r', 'time': 1}]}
    assert DeskMixin.previous_note(running, '005930', time.time(), {'bid': 1, 'ask': 1}) is None
    no_director = {'runs': [{'symbol': '005930', 'status': 'completed', 'reports': [{'role': 'critic'}], 'id': 'r', 'time': 1}]}
    assert DeskMixin.previous_note(no_director, '005930', time.time(), {'bid': 1, 'ask': 1}) is None


def test_the_memo_is_kept_short():
    state = {'runs': [{'symbol': 'A1', 'status': 'completed', 'id': 'r', 'time': time.time()-600,
                       'reports': [{'role': 'director', 'stance': 'BUY', 'summary': 'x'*5000}]}], 'evaluations': []}
    note = DeskMixin.previous_note(state, 'A1', time.time(), {'bid': 10, 'ask': 10})
    assert len(note['summary']) == 500 and note['minutes_ago'] == 10 and note['price_then'] is None and note['price_change_pct'] is None


# ---- shared helpers and the dashboard --------------------------------------------------------------------------------------

def test_trade_cost_is_fees_slippage_korean_tax_and_spread(tmp_path):
    engine, store = build(tmp_path, sell_tax_kr=20)
    quote = {'bid': 99.0, 'ask': 101.0}                        # a 2% spread on a mid of 100
    spread = 2/100*10000
    assert engine.trade_cost_bps('AAPL', quote) == pytest.approx(2*15+2*5+spread)
    assert engine.trade_cost_bps('005930', quote) == pytest.approx(2*15+2*5+20+spread)
    store.release()


def rows(count, end, step=60, **over):
    return [dict({'time': end-(count-i)*step, 'completed': True, 'volume': 1}, **over) for i in range(count)]


def test_usable_candles_need_twenty_fresh_contiguous_completed_ones():
    now = 1_000_000.0
    quote = {'session_start': now-86400}
    usable, problem = DeskMixin.usable_candles(rows(25, now-60), quote, now)
    assert problem == '' and len(usable) == 25
    assert DeskMixin.usable_candles(rows(19, now-60), quote, now)[0] is None
    assert '1분봉 20개' in DeskMixin.usable_candles(rows(25, now-400), quote, now)[1]                     # the newest is 7 minutes old
    gappy = rows(25, now-60)
    del gappy[15:18]
    assert '공백' in DeskMixin.usable_candles(gappy, quote, now)[1]
    unfinished = rows(25, now-60)
    unfinished[-1]['completed'] = False
    assert len(DeskMixin.usable_candles(unfinished, quote, now)[0]) == 24
    early = rows(30, now-60)
    assert len(DeskMixin.usable_candles(early, {'session_start': early[10]['time']}, now)[0]) == 20      # candles before the open are ignored


def test_the_dashboard_state_lists_waiting_plans_and_only_the_latest_finished_ones(day):
    now = time.time()
    with day.store.edit() as s:
        for i in range(12):
            fields = {'type': 'breakout', 'level': 1e9, 'invalidate': 1.0, 'expires': now+3600, 'plan': {}}
            watch = entry.make_watch(fields, symbol='AAPL', name='Apple', market='US', currency='USD', horizon='intraday',
                                     reference=1.0, summary='', engine='', run_id='x', generation=1, now=now+i)
            entry.close(watch, 'expired', 'x', now)
            s['watches'].append(watch)
        fields = {'type': 'breakout', 'level': 1e9, 'invalidate': 1.0, 'expires': now+3600, 'plan': {}}
        s['watches'].insert(0, entry.make_watch(fields, symbol='MSFT', name='Microsoft', market='US', currency='USD', horizon='intraday',
                                                reference=1.0, summary='', engine='', run_id='x', generation=1, now=now))
    shown = day.public_state()['watches']
    assert len(shown) == 9 and shown[0]['status'] == 'waiting' and {w['status'] for w in shown[1:]} == {'expired'}


def test_the_ledger_keeps_a_bounded_history_of_plans(day):
    now = time.time()
    with day.store.edit() as s:
        for i in range(entry.KEEP+10):
            fields = {'type': 'breakout', 'level': 1e9, 'invalidate': 1.0, 'expires': now+3600, 'plan': {}}
            old = entry.make_watch(fields, symbol='AAPL', name='Apple', market='US', currency='USD', horizon='intraday',
                                   reference=1.0, summary='', engine='', run_id=str(i), generation=1, now=now)
            entry.close(old, 'expired', 'x', now)
            s['watches'].append(old)
    watches = watched(day)['watches']
    assert len(watches) == entry.KEEP+1 and len(entry.waiting({'watches': watches})) == 1
    assert watches[0]['run_id'] == '10'                          # the oldest ten finished ones were dropped


def test_the_monitor_loop_runs_the_watches_last_so_exits_and_liquidation_never_wait_for_them():
    source = inspect.getsource(create_app)
    order = [source.index(f'engine.{name}') for name in ('refresh)', 'process_desk_exits', 'process_liquidation', 'process_entry_watches')]
    assert order == sorted(order)


def test_the_evaluation_summary_copes_with_a_conditional_entry(day):
    watched(day)
    at(day, LEVEL)
    poll(day, 2)
    evaluation = day.public_state()['evaluation']
    assert evaluation['decisions'] == 2 and evaluation['counts']['BUY'] == 1 and evaluation['counts']['HOLD'] == 1
    assert 'pending' in evaluation and evaluation['engines']


def test_right_after_the_open_one_completed_candle_is_enough():
    """2026-10-08: the first analysis of a session used to wait 20 minutes for 20 minute-candles; a month plan reads the
    daily history, so after the open one completed candle (the opening auction's minute) is enough."""
    now = 1_000_000.0
    opened = {'session_start': now-150}                                   # two and a half minutes into the session
    usable, problem = DeskMixin.usable_candles(rows(2, now-30), opened, now)
    assert problem == '' and len(usable) == 2
    assert '1분봉 1개' in DeskMixin.usable_candles([], opened, now)[1]      # the opening minute is not complete yet
    later = {'session_start': now-30*60}                                  # after 20 minutes the old rule applies again
    assert '1분봉 20개' in DeskMixin.usable_candles(rows(5, now-30), later, now)[1]
