"""The simulation follows the real 2026 cost schedule and fills the way real orders would: commission per market, Korea's
transaction tax on stock sales only (ETFs are exempt), free small US orders, and a take-profit that fills at its own limit
price while a stop sells at the market. Throw-away ledger and synthetic quotes; no market data or AI service is contacted."""
from types import SimpleNamespace

import pytest

from app.config import Config
from app.engine import Engine
from app.risk import is_etf, min_take_pct, round_trip_cost_pct, taxed, trade_fee
from app.store import Store
from test_month_desk import SETTINGS, MonthProvider, PASSWORD, SECRET, open_position

FLAT = {'bid': 100.0, 'ask': 100.0}


def real(**over):
    return SimpleNamespace(**{**dict(fee_kr=1.5, fee_us=10, sell_tax_kr=20, slippage_bps=5, min_take_cost_ratio=3), **over})


def test_the_defaults_are_the_real_2026_schedule():
    c = Config()
    assert (c.fee_kr, c.fee_us, c.sell_tax_kr, c.slippage_bps) == (1.5, 10, 20, 5)


def test_only_etfs_are_exempt_from_the_korean_transaction_tax():
    assert all(is_etf(s) for s in ('069500', '122630', 'TQQQ', 'SPY', 'QQQ'))
    assert not any(is_etf(s) for s in ('005930', '000660', 'AAPL', 'MSFT', 'NOPE'))
    assert taxed('KRW', '005930') and not taxed('KRW', '069500') and not taxed('KRW', '122630')
    assert not taxed('USD', 'AAPL') and taxed('KRW') and not taxed('USD')                   # no symbol: the conservative side


@pytest.mark.parametrize('symbol, side, gross, fee', [
    ('005930', 'BUY', 1_000_000, 150),             # 0.015%
    ('005930', 'SELL', 1_000_000, 150+2000),       # plus the 0.20% transaction tax
    ('069500', 'SELL', 1_000_000, 150),            # an ETF: no tax
    ('122630', 'SELL', 1_000_000, 150),            # a leveraged ETF is an ETF too
    ('AAPL', 'BUY', 1000, 1.0),                    # 0.1%
    ('AAPL', 'SELL', 1000, 1.0),                   # no tax in the US
    ('AAPL', 'BUY', 10, 0),                        # an order of $10 or less is free
    ('AAPL', 'SELL', 9.99, 0),
])
def test_the_fee_of_one_fill(symbol, side, gross, fee):
    assert float(trade_fee(real(), symbol, side, gross)) == pytest.approx(fee)
    assert float(trade_fee(real(), 'AAPL', 'BUY', 10.01)) == pytest.approx(10.01*.001)


def test_the_round_trip_cost_and_the_take_profit_floor_follow_the_instrument():
    assert round_trip_cost_pct(real(), 'KRW', FLAT, '005930') == pytest.approx(.33)       # 2 x 1.5 + 2 x 5 + 20 bp
    assert round_trip_cost_pct(real(), 'KRW', FLAT, '069500') == pytest.approx(.13)       # an ETF pays no tax
    assert round_trip_cost_pct(real(), 'USD', FLAT, 'AAPL') == pytest.approx(.30)         # 2 x 10 + 2 x 5 bp
    assert round_trip_cost_pct(real(), 'KRW', FLAT) == pytest.approx(.33)
    assert min_take_pct(real(), 'KRW', FLAT, '069500') == pytest.approx(.39)
    assert min_take_pct(real(), 'KRW', FLAT, '005930') == pytest.approx(.99)


@pytest.fixture
def ledger(tmp_path):
    cfg = Config(database_url='sqlite:///'+str(tmp_path/'real.db'), mode='demo', password=PASSWORD, session_secret=SECRET,
                 toss_id='', toss_secret='', gemini_key='')                                  # the real defaults
    store = Store(cfg.database_url, cfg.mode)
    engine = Engine(cfg, store, MonthProvider())
    engine.boot()
    engine.new_experiment(1_000_000, 1000, 'real costs', strategy_mode='intraday', strategy_settings=SETTINGS)
    engine.set_execution('auto')
    engine.refresh()
    engine.start()
    yield engine
    store.release()


def trade(engine, symbol, side, qty, price):
    engine.provider.prices[symbol] = price
    quote = engine.provider.quote(symbol)
    with engine.store.edit() as s:
        return engine.fill(s, symbol, side, qty, quote, 'test')


def test_a_korean_stock_round_trip_pays_commission_both_ways_and_the_tax_on_the_sale(ledger):
    buy = trade(ledger, '005930', 'BUY', 2, 70000.0)
    sell = trade(ledger, '005930', 'SELL', 2, 70000.0)
    assert buy['price'] == 70035.0 and buy['fee'] == pytest.approx(140070*.00015, abs=.01)
    assert sell['price'] == 69965.0 and sell['fee'] == pytest.approx(139930*.00215, abs=.01)
    assert sell['realized'] == pytest.approx(139930-sell['fee']-(140070+buy['fee']), abs=.02)


def test_an_etf_sale_pays_no_transaction_tax(ledger):
    trade(ledger, '069500', 'BUY', 5, 40000.0)
    sell = trade(ledger, '069500', 'SELL', 5, 40000.0)
    assert sell['fee'] == pytest.approx(5*39980*.00015, abs=.01)


def test_a_small_us_order_is_free_and_a_larger_one_is_not(ledger):
    small = trade(ledger, 'AAPL', 'BUY', .04, 200.0)                  # about $8
    large = trade(ledger, 'MSFT', 'BUY', 1, 200.0)
    assert small['fee'] == 0 and large['fee'] == pytest.approx(200.1*.001, abs=.01)


def test_a_take_profit_fills_at_its_own_limit_price_not_at_a_better_quote(ledger):
    position = open_position(ledger, '005930', price=100000.0, stop_pct=5, take_pct=10)
    assert position['take_profit_price'] == pytest.approx(110000.0)
    ledger.provider.prices['005930'] = 112000.0                        # the market ran through the target between two polls
    ledger.process_desk_exits()
    sale = ledger.store.read()['trades'][-1]
    assert (sale['side'], sale['exit_reason'], sale['price']) == ('SELL', '익절 조건', 110000.0)
    assert sale['origin'] == 'exit'


def test_a_stop_sells_at_the_market_like_a_stop_order(ledger):
    open_position(ledger, '005930', price=100000.0, stop_pct=5, take_pct=10)
    ledger.provider.prices['005930'] = 94000.0                         # gapped below the 95,000 stop
    ledger.process_desk_exits()
    sale = ledger.store.read()['trades'][-1]
    assert sale['exit_reason'] == '손절 조건' and sale['price'] == 93953.0                # the bid less the slippage assumption


def test_a_limit_fill_needs_the_market_to_reach_the_limit(ledger):
    open_position(ledger, '005930', price=100000.0)
    ledger.provider.prices['005930'] = 105000.0
    quote = ledger.provider.quote('005930')
    with pytest.raises(ValueError):
        with ledger.store.edit() as s:
            ledger.fill(s, '005930', 'SELL', 1, quote, 'early', limit=110000.0)
    with pytest.raises(ValueError):
        with ledger.store.edit() as s:
            ledger.fill(s, '005930', 'BUY', 1, quote, 'buy limit', limit=100000.0)          # only a resting sell is modelled


def test_the_scoring_cost_is_the_same_instrument_aware_estimate(ledger):
    assert ledger.trade_cost_bps('005930', FLAT) == pytest.approx(33)
    assert ledger.trade_cost_bps('069500', FLAT) == pytest.approx(13)
    assert ledger.trade_cost_bps('AAPL', FLAT) == pytest.approx(30)


def test_the_stop_risk_of_an_etf_has_no_sale_tax_in_it():
    import time
    from app.risk import size_order

    def per_share(symbol):
        state = {'strategy_settings': {'horizon': 'month'}, 'positions': {}, 'quotes': {}}
        quote = {'bid': 10000.0, 'ask': 10000.0, 'session_end': time.time()+7200}
        decision = {'stance': 'BUY', 'target_weight_pct': 30, 'stop_loss_pct': 5, 'take_profit_pct': 10, 'max_holding_minutes': 20160}
        sized = size_order(state, symbol, quote, decision, {'portfolio_equity': 10_000_000, 'max_buy_quantity': 100000}, real())
        assert sized['quantity'] > 0
        return sized['estimated_stop_risk']/sized['quantity']
    # selling at the stop (about 9,500 won after slippage) costs a stock 0.20% more in tax: about 19 won a share
    assert per_share('005930')-per_share('069500') == pytest.approx(19.0, rel=.03)
