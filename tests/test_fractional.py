"""Fractional US shares and the per-experiment limits: the ledger, the order limits and the sizing must agree to the cent.
Throw-away ledger and synthetic quotes; no market data or AI service is contacted."""
import math
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import shares
from app.agents import DESK_PROMPTS, MONTH_PROMPTS, validate_report
from app.config import Config
from app.engine import Engine, RuleError, money
from app.main import create_app
from app.risk import RiskError, normalize_settings, size_order
from app.store import Store
from test_month_desk import MonthProvider, PASSWORD, SECRET, run_cycle

SETTINGS = {'horizon': 'intraday', 'universe_mode': 'fixed', 'include_leveraged_etfs': False}


def make(tmp_path, krw=1_000_000, usd=1000, ratio=30, fractional=True, settings=None, db='frac.db'):
    config = Config(database_url='sqlite:///'+str(tmp_path/db), mode='demo', password=PASSWORD, session_secret=SECRET, toss_id='',
                    toss_secret='', gemini_key='', fee_kr=15, fee_us=15, sell_tax_kr=0, slippage_bps=5, fractional_us=fractional)
    store = Store(config.database_url, config.mode)
    engine = Engine(config, store, MonthProvider())
    engine.boot()
    engine.new_experiment(krw, usd, 'fractional', ratio, 'intraday', {**SETTINGS, **(settings or {})})
    engine.set_execution('auto')
    engine.refresh()
    engine.start()
    return engine, store


@pytest.fixture
def desk(tmp_path):
    engine, store = make(tmp_path)
    yield engine
    store.release()


def quote(engine, symbol, price, **over):
    engine.provider.prices[symbol] = price
    q = engine.provider.quote(symbol)
    q.update(over)
    return q


def buy(engine, symbol, qty, price):
    q = quote(engine, symbol, price)
    with engine.store.edit() as s:
        if symbol in s['positions']:                                # a desk position always carries a plan; without one it counts as expired
            s['positions'][symbol].setdefault('expires_at', time.time()+3600)
        trade = engine.fill(s, symbol, 'BUY', qty, q, 'test')
        s['positions'][symbol].setdefault('expires_at', time.time()+3600)
        return trade


def sell(engine, symbol, qty, price):
    q = quote(engine, symbol, price)
    with engine.store.edit() as s:
        return engine.fill(s, symbol, 'SELL', qty, q, 'test')


# ---- the helpers --------------------------------------------------------------------------------------------------------

def test_quantities_round_down_and_never_carry_noise():
    assert shares.floor_to(0.136499, True) == Decimal('0.1364') and shares.floor_to(2.99, False) == 2
    assert shares.floor_to(-3, True) == 0 and shares.floor_to(0.00009, True) == 0
    assert shares.number(Decimal('2.0000')) == 2 and isinstance(shares.number(Decimal('2.0000')), int)
    assert shares.number(Decimal('0.1364')) == 0.1364


@pytest.mark.parametrize('quantity, fractional, valid', [
    (1, True, True), (1, False, True), (0.5, True, True), (0.5, False, False), (0.1234, True, True), (0.12345, True, False),
    (0, True, False), (-1, True, False), (float('nan'), True, False), (float('inf'), True, False), (True, True, False),
    ('1', True, False), (10001, True, False), (10000, False, True), (2.0, True, True), (2.0, False, False)])
def test_what_counts_as_a_valid_quantity(quantity, fractional, valid):
    assert shares.is_valid(quantity, fractional) is valid


def test_the_minimum_order_only_applies_to_fractional_orders():
    assert shares.below_minimum(0.001, 700, True) and not shares.below_minimum(0.5, 700, True)
    assert not shares.below_minimum(1, 0.5, True) and not shares.below_minimum(0.5, 700, False)


def test_fractional_shares_are_on_by_default_and_can_be_switched_off():
    assert Config().fractional_us is True


# ---- order limits -------------------------------------------------------------------------------------------------------------

def limits(engine, symbol, price, **over):
    q = quote(engine, symbol, price, **over)
    return engine.order_constraints(engine.store.read(), symbol, q)


def test_an_expensive_us_stock_can_be_bought_in_part(desk):
    result = limits(desk, 'AAPL', 733.0)
    unit = money(733.0*1.0005)*1.0015
    assert result['fractional_shares'] and not result['integer_shares_only']
    assert result['max_buy_quantity'] == pytest.approx(math.floor((300-.02)/unit*10000)/10000, abs=1e-9)
    assert 0.39 < result['max_buy_quantity'] < 0.41                     # 30% of $1,000 buys about 0.4 share


def test_korean_shares_stay_whole_and_an_unaffordable_one_stays_at_zero(desk):
    cheap, dear = limits(desk, '005930', 70_000.0), limits(desk, '000660', 900_000.0)
    assert cheap['max_buy_quantity'] == 4 and isinstance(cheap['max_buy_quantity'], int) and cheap['integer_shares_only']
    assert dear['max_buy_quantity'] == 0 and not dear['fractional_shares']


def test_with_fractional_shares_off_us_orders_are_whole_again(tmp_path):
    engine, store = make(tmp_path, fractional=False)
    result = limits(engine, 'AAPL', 733.0)
    assert result['max_buy_quantity'] == 0 and result['integer_shares_only'] and not result['fractional_shares']
    store.release()


def test_the_book_depth_still_limits_a_fractional_order(desk):
    assert limits(desk, 'AAPL', 10.0, ask_size=3)['max_buy_quantity'] == 3


def test_an_order_below_the_minimum_amount_is_not_offered(tmp_path):
    engine, store = make(tmp_path, usd=3)                                # 30% of $3 is 90 cents
    assert limits(engine, 'AAPL', 100.0)['max_buy_quantity'] == 0
    store.release()


def test_what_is_already_held_uses_up_the_name_limit(desk):
    buy(desk, 'AAPL', 0.3, 733.0)
    left = limits(desk, 'AAPL', 733.0)['max_buy_quantity']
    assert 0 < left < 0.11                                              # about 0.4 minus the 0.3 already held


def test_the_cash_left_can_be_the_tightest_limit(tmp_path):
    engine, store = make(tmp_path, ratio=100, settings={'max_position_pct': 100})
    buy(engine, 'AAPL', 1.2, 700.0)                                     # about $841 of the $1,000 is spent
    cash = engine.store.read()['cash']['USD']
    result = limits(engine, 'MSFT', 100.0, ask_size=1000)
    unit = money(100.0*1.0005)*1.0015
    assert result['max_buy_quantity'] == pytest.approx(math.floor((cash-.02)/unit*10000)/10000, abs=1e-9)
    assert result['max_buy_quantity'] < 1.7                             # far below what 100% of the account value alone would allow (about 8)
    store.release()


def test_the_per_name_and_per_order_limits_are_the_experiments_own(tmp_path):
    engine, store = make(tmp_path, ratio=100, settings={'max_position_pct': 100})
    result = limits(engine, 'AAPL', 100.0, ask_size=1000)
    assert result['max_position_equity_ratio'] == 1 and result['max_order_equity_ratio'] == 1 and 9.9 < result['max_buy_quantity'] <= 10
    store.release()
    old, store = make(tmp_path, ratio=30, db='old.db')                   # no limit saved: the old 30%
    assert limits(old, 'AAPL', 100.0, ask_size=1000)['max_position_equity_ratio'] == .30
    store.release()


# ---- sizing ----------------------------------------------------------------------------------------------------------------------

class Cfg:
    slippage_bps, fee_kr, fee_us, sell_tax_kr = 5, 15, 15, 0


def sized(price, weight=10, stop=1.0, take=2.0, nav=1000.0, fractional=True, currency_symbol='AAPL', held=0, settings=None, max_buy=None):
    state = {'strategy_settings': {**SETTINGS, **(settings or {})}, 'positions': ({currency_symbol: {'quantity': held, 'average': price}} if held else {}), 'quotes': {}}
    constraints = {'portfolio_equity': nav, 'max_buy_quantity': max_buy if max_buy is not None else 1000, 'max_sell_quantity': held,
                   'fractional_shares': fractional}
    decision = {'stance': 'BUY', 'target_weight_pct': weight, 'stop_loss_pct': stop, 'take_profit_pct': take, 'max_holding_minutes': 90}
    quote = {'bid': price, 'ask': price, 'session_end': time.time()+7200}
    return size_order(state, currency_symbol, quote, decision, constraints, Cfg, now=time.time())


def test_the_target_weight_buys_a_fraction_of_an_expensive_share():
    result = sized(733.0, weight=10, stop=2.0, take=4.0)
    assert result['quantity'] == pytest.approx(math.floor(100/money(733.0*1.0005)*10000)/10000, abs=1e-9)
    assert 0.13 < result['quantity'] < 0.14 and isinstance(result['quantity'], float)


def test_the_risk_limit_can_be_the_tighter_one_and_is_fractional_too():
    result = sized(100.0, weight=90, stop=2.0, take=4.0, settings={'max_position_pct': 100})    # 0.5% of $1,000 = $5 of risk over about $2.4 a share
    assert 1.9 < result['quantity'] < 2.2 and result['risk_quantity'] == result['quantity']
    assert result['estimated_stop_risk'] == pytest.approx(5.0, abs=.1)


def test_whole_share_markets_still_get_whole_shares():
    result = sized(70_000.0, weight=20, stop=2.0, take=4.0, nav=1_000_000.0, fractional=False, currency_symbol='005930')
    assert isinstance(result['quantity'], int) and result['quantity'] == 2


def test_a_fractional_order_below_the_minimum_amount_is_held_back():
    result = sized(100.0, weight=50, nav=1.5, stop=2.0, take=4.0, settings={'max_position_pct': 100})       # half of $1.50 is 75 cents
    assert result['quantity'] == 0 and '최소 1달러' in result['reason']


def test_the_target_weight_is_bounded_by_the_experiments_limit():
    with pytest.raises(RiskError):
        sized(100.0, weight=50)                                          # the default limit is 30%
    assert sized(100.0, weight=50, settings={'max_position_pct': 100}, stop=2.0, take=4.0)['quantity'] > 0


def test_selling_down_to_a_lower_weight_can_sell_part_of_a_fractional_holding():
    state = {'strategy_settings': SETTINGS, 'positions': {'AAPL': {'quantity': 0.4, 'average': 733.0}}, 'quotes': {}}
    constraints = {'portfolio_equity': 1000.0, 'max_buy_quantity': 0, 'max_sell_quantity': 0.4, 'fractional_shares': True}
    quote = {'bid': 733.0, 'ask': 733.0, 'session_end': time.time()+7200}
    result = size_order(state, 'AAPL', quote, {'stance': 'SELL', 'target_weight_pct': 10}, constraints, Cfg, now=time.time())
    assert 0.25 < result['quantity'] < 0.27                              # 0.4 held, 0.1364 wanted


def test_the_settings_carry_the_position_limit_with_the_old_default():
    assert normalize_settings({})['max_position_pct'] == 30 and normalize_settings({'max_position_pct': 100})['max_position_pct'] == 100
    for bad in (9, 101, float('nan')):
        with pytest.raises(RiskError):
            normalize_settings({'max_position_pct': bad})


# ---- the ledger ---------------------------------------------------------------------------------------------------------------------

def test_a_fractional_buy_moves_exactly_the_right_cash_and_records_a_fractional_position(desk):
    trade = buy(desk, 'AAPL', 0.1364, 733.0)
    state = desk.store.read()
    price = money(733.0*1.0005)
    gross = money(price*0.1364)
    fee = money(gross*0.0015)
    assert trade['quantity'] == 0.1364 and trade['price'] == price and trade['fee'] == fee
    assert state['cash']['USD'] == money(1000-gross-fee)
    position = state['positions']['AAPL']
    assert position['quantity'] == 0.1364 and position['cost_basis'] == money(gross+fee)
    assert position['average'] == money((gross+fee)/0.1364)


def test_the_account_value_counts_a_fractional_holding_at_its_market_price(desk):
    buy(desk, 'AAPL', 0.2727, 733.0)
    desk.provider.prices['AAPL'] = 800.0
    desk.refresh()                                                      # the poll marks the position at the new price
    state = desk.store.read()
    position = state['positions']['AAPL']
    public = desk.public_state()
    expected = state['cash']['USD']+0.2727*800.0
    assert public['equity']['USD'] == pytest.approx(expected, abs=.02)
    assert public['performance']['USD']['equity'] == pytest.approx(expected, abs=.02)
    assert public['performance']['USD']['unrealized'] == pytest.approx(0.2727*800.0-position['cost_basis'], abs=.02)


def test_buying_more_averages_correctly_and_a_partial_sell_takes_its_share_of_the_cost(desk):
    buy(desk, 'AAPL', 0.1000, 700.0)
    buy(desk, 'AAPL', 0.0500, 720.0)
    position = desk.store.read()['positions']['AAPL']
    assert position['quantity'] == 0.15
    assert position['average'] == money(position['cost_basis']/0.15)
    before = desk.store.read()
    trade = sell(desk, 'AAPL', 0.0500, 730.0)
    after = desk.store.read()
    assert after['positions']['AAPL']['quantity'] == 0.1 and trade['side'] == 'SELL'
    assert after['positions']['AAPL']['cost_basis'] == money(position['cost_basis']-money(position['cost_basis']*0.05/0.15))
    assert after['cash']['USD'] > before['cash']['USD']


def test_a_full_sell_closes_the_position_and_nothing_is_left_over(desk):
    buy(desk, 'AAPL', 0.2727, 733.0)
    trade = sell(desk, 'AAPL', 0.2727, 733.0)
    state = desk.store.read()
    assert 'AAPL' not in state['positions']
    assert state['cash']['USD'] == pytest.approx(1000+trade['realized'], abs=.02)
    assert trade['realized'] < 0                                        # a round trip at the same quote only costs money


def test_a_round_trip_never_creates_money_whatever_the_size(desk):
    start = desk.store.read()['cash']['USD']
    for quantity in (0.0137, 0.2, 0.3333, 0.05):
        buy(desk, 'AAPL', quantity, 250.0)
        sell(desk, 'AAPL', quantity, 250.0)
        assert desk.store.read()['cash']['USD'] < start
        start = desk.store.read()['cash']['USD']
    assert not desk.store.read()['positions']


@pytest.mark.parametrize('bad', [0.00001, 0.12345, 0, -0.5, float('nan'), float('inf'), True, '0.5', 10001])
def test_invalid_fractional_quantities_are_refused(desk, bad):
    with pytest.raises(RuleError):
        buy(desk, 'AAPL', bad, 100.0)
    assert not desk.store.read()['positions'] and desk.store.read()['cash']['USD'] == 1000


def test_korean_orders_must_stay_whole_shares(desk):
    with pytest.raises(RuleError):
        buy(desk, '005930', 1.5, 70_000.0)
    assert buy(desk, '005930', 2, 70_000.0)['quantity'] == 2


def test_us_orders_are_whole_only_when_fractional_shares_are_off(tmp_path):
    engine, store = make(tmp_path, fractional=False)
    with pytest.raises(RuleError):
        buy(engine, 'AAPL', 0.5, 100.0)
    store.release()


def test_a_fractional_buy_below_the_minimum_amount_is_refused_but_the_rest_can_always_be_sold(desk):
    with pytest.raises(RuleError, match='최소 1달러'):
        buy(desk, 'AAPL', 0.001, 700.0)                                  # 70 cents
    buy(desk, 'AAPL', 0.0015, 700.0)                                     # $1.05
    with pytest.raises(RuleError, match='최소 1달러'):
        sell(desk, 'AAPL', 0.0005, 700.0)                                # a partial sell under a dollar
    sell(desk, 'AAPL', 0.0015, 700.0)                                    # the whole remainder is exempt
    assert not desk.store.read()['positions']


def test_the_name_limit_stops_a_fractional_buy_that_would_pass_it(desk):
    with pytest.raises(RuleError, match='30%'):
        buy(desk, 'AAPL', 0.45, 733.0)


def test_a_higher_name_limit_lets_a_bigger_fractional_position_in(tmp_path):
    engine, store = make(tmp_path, ratio=100, settings={'max_position_pct': 50})
    assert buy(engine, 'AAPL', 0.6, 700.0)['quantity'] == 0.6          # 42% of the account: past the old 30%, inside this experiment's 50%
    with pytest.raises(RuleError, match='50%'):
        buy(engine, 'AAPL', 0.2, 700.0)                                  # 56% would pass it
    store.release()


def test_the_experiment_form_accepts_a_per_order_limit_up_to_100_and_no_more(tmp_path):
    engine, store = make(tmp_path, ratio=100, db='hundred.db')
    assert engine.store.read()['max_order_ratio'] == 1
    store.release()
    config = Config(database_url='sqlite:///'+str(tmp_path/'x.db'), mode='demo', password=PASSWORD, session_secret=SECRET, toss_id='', toss_secret='', gemini_key='')
    other = Engine(config, Store(config.database_url, config.mode), MonthProvider())
    other.boot()
    with pytest.raises(RuleError):
        other.new_experiment(1000, 1000, 'x', 101, 'intraday', SETTINGS)
    other.store.release()


# ---- the desk ----------------------------------------------------------------------------------------------------------------------

def test_the_desk_buys_a_fraction_of_an_expensive_stock_and_sells_all_of_it_at_the_stop(desk):
    desk.provider.prices['AAPL'] = 733.0
    desk.request_cycle('AAPL')
    state = run_cycle(desk)
    trade = state['trades'][-1]
    assert trade['side'] == 'BUY' and trade['symbol'] == 'AAPL' and 0 < trade['quantity'] < 1
    position = state['positions']['AAPL']
    assert position['quantity'] == trade['quantity'] and position['stop_price'] < 733.0
    desk.provider.prices['AAPL'] = position['stop_price']-1
    desk.process_desk_exits()
    state = desk.store.read()
    assert 'AAPL' not in state['positions'] and state['trades'][-1]['quantity'] == trade['quantity']
    assert state['trades'][-1]['exit_reason'] == '손절 조건'


def test_fractional_orders_leave_no_shadow_record_and_no_warning_but_whole_ones_still_do(desk):
    desk.provider.prices['AAPL'] = 733.0
    desk.request_cycle('AAPL')
    state = run_cycle(desk)
    assert not [r for r in state.get('shadow_orders', []) if r['symbol'] == 'AAPL']
    assert not any('그림자 기록에 실패' in e['message'] for e in state['events'])
    desk.provider.prices['005930'] = 70_000.0
    desk.request_cycle('005930')
    state = run_cycle(desk)
    assert [r for r in state.get('shadow_orders', []) if r['symbol'] == '005930']


def test_the_analysts_are_told_the_limits_and_that_us_orders_can_be_fractional(desk):
    seen = []
    real = desk.agents.run
    desk.agents.run = lambda role, ctx, gen: (seen.append((role, ctx.get('constraints'), ctx.get('strategy_settings'))), real(role, ctx, gen))[1]
    desk.provider.prices['AAPL'] = 733.0
    desk.request_cycle('AAPL')
    run_cycle(desk)
    role, constraints, settings = next(x for x in seen if x[0] == 'director')
    assert constraints['fractional_shares'] is True and constraints['max_position_equity_ratio'] == .30 and settings['max_position_pct'] == 30
    for prompt in (DESK_PROMPTS['director'], MONTH_PROMPTS['director']):
        assert 'max_position_pct' in prompt and '소수점' in prompt


def test_the_directors_weight_may_use_the_experiments_higher_limit_only(desk):
    report = {'summary': 's', 'stance': 'BUY', 'quantity': 1, 'risks': [], 'target_weight_pct': 60, 'stop_loss_pct': 2, 'take_profit_pct': 4,
              'max_holding_minutes': 60, 'tasks': [], 'evidence': []}
    with pytest.raises(ValueError):
        validate_report(report, 'critic', {'strategy_settings': {'horizon': 'intraday'}, 'reports': []}, [], desk=True)
    ok = validate_report(report, 'critic', {'strategy_settings': {'horizon': 'intraday', 'max_position_pct': 100}, 'reports': []}, [], desk=True)
    assert ok['stance'] == 'HOLD'


# ---- the public form ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'api.db'), mode='demo', password=PASSWORD, session_secret=SECRET, toss_id='', toss_secret='', gemini_key='')
    with TestClient(create_app(config, background=False, test=True)) as c:
        c.headers.update({'X-Stocklab-Action': '1'})
        assert c.post('/api/login', json={'password': PASSWORD}).status_code == 200
        yield c


def experiment(**over):
    body = {'seed_krw': 10_000_000, 'seed_usd': 10_000, 'name': 'x', 'max_order_pct': 30, 'strategy_mode': 'intraday',
            'include_leveraged_etfs': False, 'risk_per_trade_pct': .5, 'daily_loss_limit_pct': 2, 'confirmation': '새 실험 시작'}
    body.update(over)
    return body


def test_the_position_limit_defaults_by_investment_horizon_and_can_be_set(client):
    assert client.post('/api/experiments', json=experiment(horizon='intraday', max_holding_minutes=120)).status_code == 200
    assert client.get('/api/state').json()['strategy_settings']['max_position_pct'] == 100
    assert client.post('/api/experiments', json=experiment()).status_code == 200
    assert client.get('/api/state').json()['strategy_settings']['max_position_pct'] == 30
    assert client.post('/api/experiments', json=experiment(horizon='intraday', max_holding_minutes=120, max_position_pct=50, max_order_pct=100)).status_code == 200
    state = client.get('/api/state').json()
    assert state['strategy_settings']['max_position_pct'] == 50 and state['config'] is not None
    for bad in ({'max_position_pct': 5}, {'max_position_pct': 101}, {'max_order_pct': 101}, {'max_order_pct': 0}):
        assert client.post('/api/experiments', json=experiment(**bad)).status_code == 422
