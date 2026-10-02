"""The month-horizon desk end to end on a throw-away ledger with synthetic quotes and daily bars: the rule-signal gate,
what the analysts are shown, the plan that gets stored, the trailing stop and the exits, and trading-day scoring."""
import copy
import math
import time

import pytest
from fastapi.testclient import TestClient

from app import evaluation
from app.config import Config
from app.engine import Engine
from app.instruments import SYMBOLS
from app.main import create_app
from app.providers import DemoProvider, ProviderError
from app.rules import signals
from app.store import Store

DAY = 86400
PASSWORD, SECRET = 'test-password-123456', 'test-secret-123456789012345678901234'


def bars(closes, spread=.005, volume=1_000_000):
    end = int(time.time()//DAY)*DAY
    n = len(closes)
    return [{'time': end-(n-i)*DAY, 'open': c, 'high': c*(1+spread), 'low': c*(1-spread), 'close': c, 'volume': volume,
             'currency': 'KRW', 'interval': '1d', 'completed': True} for i, c in enumerate(closes)]


def series(base, kind):
    if kind == 'up':
        return [base*(1+.006*i)*(1+.006*math.sin(i*1.7)) for i in range(70)]
    if kind == 'down':
        return [2*base*(1-.006*i)*(1+.006*math.sin(i*1.7)) for i in range(70)]
    return [base*(1+.003*math.sin(i*.4)) for i in range(70)]


def test_the_synthetic_series_mean_what_the_tests_say_they_mean():
    assert signals(bars(series(100, 'up')))['momentum'] == 'BUY'
    assert signals(bars(series(100, 'down')))['momentum'] == 'SELL'
    assert set(signals(bars(series(100, 'quiet'))).values()) == {'HOLD'}


class MonthProvider(DemoProvider):
    """Fixed prices, a movable session end, and daily bars that default to a quiet market (no rule fires)."""

    def __init__(self):
        self.prices, self.daily, self.fail_daily, self.session_end = {}, {}, set(), None
        self.closed, self.quote_calls = set(), []

    def quote(self, symbol):
        q = super().quote(symbol)
        self.quote_calls.append(symbol)
        if symbol in self.closed:
            q['tradable'] = False
        if symbol in self.prices:
            q.update(last=self.prices[symbol], bid=self.prices[symbol], ask=self.prices[symbol])
        if self.session_end is not None:
            q['session_end'] = self.session_end
        return q

    def candles(self, symbol, interval='1d', count=None):
        if interval != '1d':
            return super().candles(symbol, interval, count)
        if symbol in self.fail_daily:
            raise ProviderError('daily bars unavailable')
        return self.daily.get(symbol) or bars(series(SYMBOLS[symbol]['demo_base'], 'quiet'))


SETTINGS = {'horizon': 'month', 'universe_mode': 'fixed', 'include_leveraged_etfs': False}


@pytest.fixture
def desk(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'month.db'), mode='demo', password=PASSWORD, session_secret=SECRET,
                    toss_id='', toss_secret='', gemini_key='', fee_kr=15, fee_us=15, sell_tax_kr=0, slippage_bps=5)
    store = Store(config.database_url, config.mode)
    engine = Engine(config, store, MonthProvider())
    engine.signal_gate = True                        # demo answers are scripted, but the rule under test is not
    engine.boot()
    engine.new_experiment(1000000, 1000, 'month test', strategy_mode='intraday', strategy_settings=SETTINGS)
    engine.set_execution('auto')
    engine.refresh()
    engine.calls, engine.contexts = [], []
    real = engine.agents.run

    def spy(role, context, generation):
        engine.calls.append(role)
        engine.contexts.append((role, copy.deepcopy(context)))
        return real(role, context, generation)
    engine.agents.run = spy
    engine.start()
    yield engine
    store.release()


def run_cycle(engine):
    with engine.store.edit() as s:
        s['next_run'] = 0
    engine.cycle()
    return engine.store.read()


def open_position(engine, symbol='005930', price=100000.0, stop_pct=5, take_pct=10, expires_in=14*DAY, quantity=2):
    engine.provider.prices[symbol] = price
    quote = engine.provider.quote(symbol)
    with engine.store.edit() as s:
        engine.fill(s, symbol, 'BUY', quantity, quote, 'test-entry')
        engine.apply_desk_plan(s, symbol, {'side': 'BUY', 'summary': 'test', 'stop_loss_pct': stop_pct},
                               {'stop_price': price*(1-stop_pct/100), 'take_profit_price': price*(1+take_pct/100),
                                'expires_at': time.time()+expires_in, 'quantity': quantity})
    return engine.store.read()['positions'][symbol]


# ---- the gate -----------------------------------------------------------------------------------------------------------

def test_with_no_rule_signal_the_cycle_makes_no_ai_call_and_waits_a_full_interval(desk):
    state = run_cycle(desk)
    assert desk.calls == [] and state['runs'] == []
    gate = state['desk_gate']
    assert gate['skipped'] and gate['skipped_cycles'] == 1 and {c['reason'] for c in gate['checked']} == {'no_signal'}
    assert state['scheduler_status'].startswith('규칙 신호가 없어 AI를 호출하지 않았습니다')
    assert desk.c.interval_seconds-5 <= state['next_run']-time.time() <= desk.c.interval_seconds+1
    state = run_cycle(desk)
    assert state['desk_gate']['skipped_cycles'] == 2 and desk.calls == []
    assert sum('규칙 신호가 없어 AI 호출 없이 대기합니다' in e['message'] for e in state['events']) == 1     # logged once, not every time


def test_the_default_analysis_interval_is_twenty_minutes():
    assert Config().interval_seconds == 1200


def test_a_buy_signal_analyses_that_name_only_and_records_the_month_decision(desk):
    desk.provider.daily['005930'] = bars(series(70000, 'up'))
    state = run_cycle(desk)
    run = state['runs'][-1]
    assert run['symbol'] == '005930' and run['horizon'] == 'month' and run['status'] == 'completed'
    assert desk.calls[0] == 'planner' and desk.calls[-1] == 'director' and 'selector' not in desk.calls      # one candidate: no selection call
    assert sorted(desk.calls[1:-2]) == ['fundamental', 'news', 'technical'] and len(desk.calls) == 6
    record = state['evaluations'][-1]
    assert record['horizon'] == 'month' and record['rules']['momentum'] == 'BUY'
    checked = {c['symbol']: c for c in state['desk_gate']['checked']}
    assert checked['005930']['eligible'] and checked['005930']['rules'] == ['momentum']
    assert not checked['000660']['eligible'] and not state['desk_gate']['skipped']


def test_the_analysts_get_three_months_of_daily_bars_and_the_reason_they_were_asked(desk):
    desk.provider.daily['005930'] = bars(series(70000, 'up'))
    run_cycle(desk)
    context = dict(desk.contexts)['technical']
    assert context['horizon'] == 'month' and context['strategy_settings']['horizon'] == 'month'
    daily = context['daily_history']
    assert 60 <= len(daily['rows']) <= 70 and daily['summary']['sessions'] == len(daily['rows'])
    assert daily['columns'] == ['date', 'open', 'high', 'low', 'close', 'volume'] and daily['summary']['ret_3m_pct'] is not None
    assert context['rule_signals']['momentum'] == 'BUY'
    assert context['rule_triggers'] == {'side': 'BUY', 'why': 'signal', 'rules': ['모멘텀']}
    assert set(context['own_history']) == {'decisions', 'trades'}
    tape = context['intraday']                                                    # the live tape: 30 compact rows, sent once
    assert tape['interval'] == '1m' and 1 <= len(tape['rows']) <= 30 and len(tape['rows'][0]) == len(tape['columns'])
    assert 'candles' not in context and 'intraday_candles' not in context


def test_the_selector_compares_daily_facts_and_rule_signals_when_several_names_qualify(desk):
    for symbol in ('005930', '000660'):
        desk.provider.daily[symbol] = bars(series(SYMBOLS[symbol]['demo_base'], 'up'))
    run_cycle(desk)
    ctx = dict(desk.contexts)['selector']
    assert ctx['strategy_settings']['horizon'] == 'month'
    candidates = {c['symbol']: c for c in ctx['candidates']}
    assert set(candidates) == {'005930', '000660'}
    for candidate in candidates.values():
        assert candidate['rule_signals']['momentum'] == 'BUY' and candidate['rule_triggers'] == ['모멘텀']
        assert candidate['daily']['ret_1m_pct'] is not None and 'atr_pct' in candidate['daily']


def test_the_same_name_is_not_analysed_again_within_hours_unless_it_moves(desk):
    desk.stop()
    desk.set_execution('manual')                       # nothing is bought, so the name stays "not held"
    desk.start()
    desk.provider.daily['005930'] = bars(series(70000, 'up'))
    desk.provider.prices['005930'] = 70000.0
    run_cycle(desk)
    first = len(desk.calls)
    assert first == 6
    state = run_cycle(desk)
    assert len(desk.calls) == first
    assert {c['symbol']: c['reason'] for c in state['desk_gate']['checked']}['005930'] == 'recent'
    desk.provider.prices['005930'] = 70000.0*1.04
    desk.refresh()                                     # the next poll sees the move
    run_cycle(desk)
    assert len(desk.calls) == first+6


def test_a_held_name_is_only_reviewed_on_a_sell_signal(desk):
    open_position(desk)
    desk.provider.daily['005930'] = bars(series(100000, 'up'))      # a BUY signal is no reason to look at a name we already hold
    state = run_cycle(desk)
    assert desk.calls == [] and {c['symbol']: c['reason'] for c in state['desk_gate']['checked']}['005930'] == 'no_signal'
    desk.provider.daily['005930'] = bars(series(100000, 'down'))
    desk.daily_cache.clear()
    state = run_cycle(desk)
    assert desk.calls and state['runs'][-1]['symbol'] == '005930'
    assert dict(desk.contexts)['director']['rule_triggers']['side'] == 'SELL'


def test_asking_for_a_name_bypasses_the_gate(desk):
    desk.request_cycle('005930')
    state = run_cycle(desk)
    assert state['runs'][-1]['symbol'] == '005930' and state['runs'][-1]['selected_by'] == 'user' and desk.calls
    assert {c['symbol']: c['reason'] for c in state['desk_gate']['checked']}['005930'] == 'requested'


def test_missing_daily_bars_never_count_as_a_signal(desk):
    desk.provider.fail_daily.update(['005930', '000660', 'AAPL', 'MSFT'])
    state = run_cycle(desk)
    assert desk.calls == [] and {c['reason'] for c in state['desk_gate']['checked']} == {'no_data'}
    assert state['desk_gate']['skipped']


def test_too_few_bars_for_the_rules_are_treated_as_no_data(desk):
    desk.provider.daily['005930'] = bars(series(70000, 'up'))[-10:]
    state = run_cycle(desk)
    assert desk.calls == [] and {c['symbol']: c['reason'] for c in state['desk_gate']['checked']}['005930'] == 'no_data'


def test_leaving_the_waiting_state_is_logged_once(desk):
    run_cycle(desk)
    desk.provider.daily['005930'] = bars(series(70000, 'up'))
    desk.daily_cache.clear()
    state = run_cycle(desk)
    assert sum('규칙 신호가 나타나' in e['message'] for e in state['events']) == 1
    assert state['desk_gate']['skipped'] is False


def test_a_market_the_poll_saw_closed_is_not_asked_about_again(desk):
    desk.provider.closed = {'AAPL', 'MSFT'}
    desk.refresh()                                      # the poll records the closed quotes
    desk.provider.quote_calls.clear()
    run_cycle(desk)
    state = desk.store.read()
    assert 'AAPL' not in desk.provider.quote_calls and 'MSFT' not in desk.provider.quote_calls
    # the open market is still checked, on the quotes the poll just fetched (no second call for them either)
    assert {c['symbol'] for c in state['desk_gate']['checked']} == {'005930', '000660'}


def test_the_older_same_session_mode_is_not_gated(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'old.db'), mode='demo', password=PASSWORD, session_secret=SECRET,
                    toss_id='', toss_secret='', gemini_key='')
    store = Store(config.database_url, config.mode)
    engine = Engine(config, store, MonthProvider())
    engine.signal_gate = True
    engine.boot()
    engine.new_experiment(1000000, 1000, 'old', strategy_mode='intraday', strategy_settings={'horizon': 'intraday'})
    engine.set_execution('auto')
    engine.refresh()
    engine.start()
    state = run_cycle(engine)
    assert state['runs'][-1]['horizon'] == 'intraday' and state['runs'][-1]['status'] == 'completed'
    assert state['desk_gate'] == {}
    store.release()


# ---- the stored plan and the exits --------------------------------------------------------------------------------------

def test_a_filled_month_plan_is_held_overnight_with_a_trailing_plan(desk):
    desk.provider.daily['005930'] = bars(series(70000, 'up'))
    state = run_cycle(desk)
    position = state['positions']['005930']
    assert position['horizon'] == 'month' and position['trail_pct'] == 5 and position['initial_stop'] == position['stop_price']
    assert position['expires_at']-time.time() == pytest.approx(20160*60, abs=120)
    assert state['trades'][-1]['side'] == 'BUY'


def test_a_month_position_is_not_closed_before_the_session_ends_but_a_same_session_one_is(desk):
    month = open_position(desk, '005930')
    desk.provider.session_end = time.time()+60                        # the bell is a minute away
    desk.process_desk_exits()
    assert desk.store.read()['positions']['005930']['quantity'] == 2 and month['horizon'] == 'month'
    with desk.store.edit() as s:
        s['positions']['005930'].pop('horizon')                        # a position from before the month mode existed
    desk.process_desk_exits()
    state = desk.store.read()
    assert not state['positions'] and state['trades'][-1]['exit_reason'] == '장 마감 전 청산'


def test_the_stop_follows_the_price_up_and_sells_on_a_pullback(desk):
    position = open_position(desk)
    arm = position['average']+(position['take_profit_price']-position['average'])*.5
    original = position['stop_price']
    desk.provider.prices['005930'] = arm-500                           # not yet half way: nothing changes
    desk.process_desk_exits()
    assert desk.store.read()['positions']['005930']['stop_price'] == original
    desk.provider.prices['005930'] = 106000.0                          # half way and beyond: the stop rises above the entry
    desk.process_desk_exits()
    raised = desk.store.read()['positions']['005930']
    assert raised['trailing'] and raised['stop_price'] > position['average'] and raised['high_water'] == 106000.0
    assert raised['stop_price'] == pytest.approx(106000*.95, abs=1)
    desk.provider.prices['005930'] = 109000.0                          # a higher high lifts it again
    desk.process_desk_exits()
    higher = desk.store.read()['positions']['005930']['stop_price']
    assert higher > raised['stop_price']
    desk.provider.prices['005930'] = 106500.0                          # a pullback that stays above the raised stop
    desk.process_desk_exits()
    assert desk.store.read()['positions']['005930']['stop_price'] == higher
    desk.provider.prices['005930'] = higher-100                        # ... and one that breaks it
    desk.process_desk_exits()
    state = desk.store.read()
    assert not state['positions'] and state['trades'][-1]['exit_reason'] == '추적 손절(이익 보호)'
    assert state['trades'][-1]['price'] > position['average']          # the trailing stop locked in a profit


def test_a_position_that_never_gained_still_exits_on_its_first_stop(desk):
    position = open_position(desk)
    desk.provider.prices['005930'] = position['stop_price']-1
    desk.process_desk_exits()
    state = desk.store.read()
    assert not state['positions'] and state['trades'][-1]['exit_reason'] == '손절 조건'


def test_target_and_holding_limit_still_close_a_month_position(desk):
    position = open_position(desk)
    desk.provider.prices['005930'] = position['take_profit_price']+10
    desk.process_desk_exits()
    assert desk.store.read()['trades'][-1]['exit_reason'] == '익절 조건'
    open_position(desk, '000660', 180000.0, expires_in=-10, quantity=1)
    desk.provider.prices['000660'] = 180000.0
    desk.process_desk_exits()
    assert desk.store.read()['trades'][-1]['exit_reason'] == '최대 보유시간'


def test_adding_to_a_month_position_never_loosens_the_stop_or_the_trail(desk):
    first = open_position(desk, price=50000.0, stop_pct=5)
    with desk.store.edit() as s:
        quote = desk.provider.quote('005930')
        desk.fill(s, '005930', 'BUY', 1, quote, 'add')
        desk.apply_desk_plan(s, '005930', {'side': 'BUY', 'summary': 'add', 'stop_loss_pct': 9},
                             {'stop_price': 45000.0, 'take_profit_price': 62500.0, 'expires_at': time.time()+30*DAY, 'quantity': 1})
    after = desk.store.read()['positions']['005930']
    assert after['stop_price'] == first['stop_price'] and after['trail_pct'] == 5
    assert after['take_profit_price'] == first['take_profit_price'] and after['expires_at'] == first['expires_at']
    assert after['initial_stop'] == first['initial_stop']


# ---- scoring on later daily closes --------------------------------------------------------------------------------------

def test_the_engine_reads_daily_bars_and_scores_month_decisions_later(desk):
    rows = bars(series(70000, 'up'))
    desk.provider.daily['005930'] = rows
    decided = rows[-8]['time']+3600                      # eight trading days before the last bar
    with desk.store.edit() as s:
        s.setdefault('evaluations', []).append({'run_id': 'old', 'time': decided, 'symbol': '005930', 'market': 'KR', 'horizon': 'month',
                                 'stance': 'BUY', 'price': rows[-8]['close'], 'cost_bps': 20, 'candidates': {}, 'rules': {},
                                 'outcomes': {}, 'selected_by': 'ai', 'engine': 'x', 'action': 'hold'})
    desk.score_days(decided+9*DAY)
    outcomes = desk.store.read()['evaluations'][-1]['outcomes']
    assert set(outcomes) == {'d1', 'd5'}
    assert outcomes['d1']['returns']['005930'] == pytest.approx((rows[-7]['close']/rows[-8]['close']-1)*100, abs=1e-3)
    assert outcomes['d5']['returns']['005930'] == pytest.approx((rows[-3]['close']/rows[-8]['close']-1)*100, abs=1e-3)
    calls = len(desk.provider.__dict__)                  # a second pass right away does nothing (rate limited)
    desk.score_days(decided+9*DAY+60)
    assert desk.store.read()['evaluations'][-1]['outcomes'] == outcomes and calls == len(desk.provider.__dict__)


def test_a_cycle_leaves_no_thirty_minute_scoring_for_month_decisions(desk):
    desk.provider.daily['005930'] = bars(series(70000, 'up'))
    run_cycle(desk)
    desk.refresh()
    assert desk.store.read()['evaluations'][-1]['outcomes'] == {}


# ---- the public surface -------------------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'api.db'), mode='demo', password=PASSWORD, session_secret=SECRET,
                    toss_id='', toss_secret='', gemini_key='')
    with TestClient(create_app(config, background=False, test=True)) as c:
        c.headers.update({'X-Stocklab-Action': '1'})
        assert c.post('/api/login', json={'password': PASSWORD}).status_code == 200
        yield c


def experiment(**over):
    body = {'seed_krw': 1000000, 'seed_usd': 1000, 'name': 'x', 'max_order_pct': 30, 'strategy_mode': 'intraday',
            'include_leveraged_etfs': False, 'risk_per_trade_pct': .5, 'daily_loss_limit_pct': 2, 'confirmation': '새 실험 시작'}
    body.update(over)
    return body


def test_a_new_experiment_is_a_month_swing_by_default(client):
    assert client.post('/api/experiments', json=experiment()).status_code == 200
    settings = client.get('/api/state').json()['strategy_settings']
    assert settings['horizon'] == 'month' and settings['max_holding_minutes'] == 43200


def test_the_holding_limit_must_fit_a_month_plan_and_day_trading_can_no_longer_be_started(client):
    assert client.post('/api/experiments', json=experiment(max_holding_minutes=10080)).status_code == 200
    assert client.get('/api/state').json()['strategy_settings']['max_holding_minutes'] == 10080
    assert client.post('/api/experiments', json=experiment(max_holding_minutes=120)).status_code == 409        # too short for a month plan
    refused = client.post('/api/experiments', json=experiment(horizon='intraday', max_holding_minutes=120))
    assert refused.status_code == 409 and '단타' in refused.text                                              # removed 2026-10-01
    assert client.get('/api/state').json()['strategy_settings']['horizon'] == 'month'
    assert client.post('/api/experiments', json=experiment(horizon='year')).status_code == 422
    assert client.post('/api/experiments', json=experiment(max_holding_minutes=50000)).status_code == 422


# ---- why a name was not analysed ----------------------------------------------------------------------------------------------

def test_the_gate_says_why_daily_data_is_missing(desk):
    desk.provider.fail_daily.add('005930')
    desk.provider.daily['000660'] = bars(series(180000, 'quiet')[:10])
    state = run_cycle(desk)
    checked = {c['symbol']: c for c in state['desk_gate']['checked']}
    assert checked['005930']['reason'] == 'no_data' and checked['005930']['detail'] == 'daily bars unavailable'
    assert checked['000660']['detail'] == '완료된 일봉 10개 (필요 23개)'
    assert 'detail' not in checked.get('AAPL', {}) and desk.calls == []


def test_the_desk_records_why_each_name_was_left_out_of_a_round(desk, monkeypatch):
    real = desk.provider.candles

    def candles(symbol, interval='1d', count=None):
        if symbol == '000660' and interval == '1m':
            raise ProviderError('캔들 데이터의 시각·통화·가격을 검증하지 못했습니다.')
        return real(symbol, interval, count)
    monkeypatch.setattr(desk.provider, 'candles', candles)
    state = run_cycle(desk)
    scan = state['desk_scan']
    assert scan['skipped']['000660'].startswith('캔들 데이터') and '005930' not in scan['skipped'] and scan['repairs'] == {}
    desk.provider.closed = {'005930', '000660', 'AAPL', 'MSFT'}
    desk.refresh()
    state = run_cycle(desk)                                    # nothing to analyse: the scan is still saved with the wait
    assert state['desk_scan']['time'] > scan['time'] and state['desk_scan']['skipped'] == {}


def test_a_name_without_enough_minute_candles_is_listed_with_the_reason(desk, monkeypatch):
    real = desk.provider.candles
    monkeypatch.setattr(desk.provider, 'candles', lambda symbol, interval='1d', count=None:
                        real(symbol, interval, count)[-5:] if symbol == '000660' and interval == '1m' else real(symbol, interval, count))
    state = run_cycle(desk)
    assert state['desk_scan']['skipped']['000660'].startswith('당일 완료된 1분봉 20개')


def test_repaired_candle_data_is_reported(desk):
    desk.provider.candle_notes = {'005930:1d': {'time': time.time(), 'repaired': 2, 'dropped': 1, 'kept': 69},
                                  'AAPL:1m': {'time': time.time(), 'repaired': 0, 'dropped': 0, 'kept': 120},
                                  'MSFT:1d': {'time': time.time()-7200, 'repaired': 5, 'dropped': 0, 'kept': 70}}
    state = run_cycle(desk)
    assert state['desk_scan']['repairs'] == {'005930:1d': {'time': desk.provider.candle_notes['005930:1d']['time'], 'repaired': 2,
                                                           'dropped': 1, 'kept': 69}}


# ---- fewer duplicate quote calls ----------------------------------------------------------------------------------------------

def test_choosing_candidates_uses_the_quotes_the_poll_just_fetched(desk):
    desk.refresh()
    desk.provider.quote_calls.clear()
    run_cycle(desk)                                            # quiet daily bars: the gate skips, so no order quote either
    assert desk.provider.quote_calls == []
    with desk.store.edit() as s:
        for quote in s['quotes'].values():
            quote['received'] -= 20                            # older than the poll normally leaves them
    run_cycle(desk)
    assert sorted(set(desk.provider.quote_calls)) == ['000660', '005930', 'AAPL', 'MSFT']


def test_a_polled_quote_must_still_be_valid(desk):
    desk.refresh()
    with desk.store.edit() as s:
        s['quotes']['005930']['tradable'] = False
    with pytest.raises(ValueError):
        desk.polled_quote(desk.store.read(), '005930')


def test_exits_do_not_ask_toss_about_a_closed_market(desk):
    open_position(desk, '005930', price=100000.0)
    desk.provider.closed.add('005930')
    desk.refresh()
    desk.provider.quote_calls.clear()
    desk.process_desk_exits()
    assert '005930' not in desk.provider.quote_calls
    desk.provider.closed.discard('005930')
    desk.refresh()
    desk.provider.quote_calls.clear()
    desk.provider.prices['005930'] = 94000.0                   # open again and below the stop: sold on a fresh quote
    desk.process_desk_exits()
    assert desk.provider.quote_calls == ['005930'] and desk.store.read()['trades'][-1]['exit_reason'] == '손절 조건'
