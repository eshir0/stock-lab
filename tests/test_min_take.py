"""A buy whose take-profit is narrower than a few times the round-trip cost is refused: the costs would eat the target. The rule is the
server's (risk.size_order), the AI is told the numbers before it decides, and a conditional-entry plan below the floor is not kept.
Pure functions plus a throw-away ledger with synthetic quotes; no market data or AI service is contacted."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import entry
from app.agents import DESK_PROMPTS, MONTH_PROMPTS
from app.config import Config
from app.risk import RiskError, min_take_pct, round_trip_cost_pct, size_order
from test_entry_desk import LEVEL, analyse, at, build, poll, watched

NOW = 1_800_000_000
QUOTE = {'bid': 100.0, 'ask': 100.0, 'session_end': NOW+7200}
JS = (Path(__file__).resolve().parents[1]/'app'/'static'/'app.js').read_text(encoding='utf-8')


def cfg(**over):
    base = dict(slippage_bps=5, fee_kr=15, fee_us=15, sell_tax_kr=0, min_take_cost_ratio=3.0)
    base.update(over)
    return SimpleNamespace(**base)


# ---- the cost and the floor ---------------------------------------------------------------------------------------------------

def test_the_round_trip_cost_is_both_fees_both_slippages_the_korean_tax_and_the_spread():
    assert round_trip_cost_pct(cfg(), 'KRW', QUOTE) == pytest.approx(.40)                     # 2 x 15 + 2 x 5 bp
    assert round_trip_cost_pct(cfg(sell_tax_kr=18), 'KRW', QUOTE) == pytest.approx(.58)       # the sell tax is Korean only
    assert round_trip_cost_pct(cfg(sell_tax_kr=18), 'USD', QUOTE) == pytest.approx(.40)
    assert round_trip_cost_pct(cfg(fee_us=25), 'USD', QUOTE) == pytest.approx(.60)
    assert round_trip_cost_pct(cfg(fee_us=25), 'KRW', QUOTE) == pytest.approx(.40)
    assert round_trip_cost_pct(cfg(slippage_bps=20), 'KRW', QUOTE) == pytest.approx(.70)
    assert round_trip_cost_pct(cfg(), 'KRW', {'bid': 99.0, 'ask': 101.0}) == pytest.approx(.40+2.0)   # a 2% spread on a mid of 100


@pytest.mark.parametrize('bad', [{'bid': 0, 'ask': 100.0}, {'bid': 100.0, 'ask': None}, {'ask': 100.0}])
def test_a_quote_without_a_usable_book_has_no_cost_estimate(bad):
    with pytest.raises(RiskError):
        round_trip_cost_pct(cfg(), 'KRW', bad)


def test_the_floor_is_the_ratio_times_the_cost_and_zero_when_the_rule_is_off():
    assert min_take_pct(cfg(), 'KRW', QUOTE) == pytest.approx(1.2)
    assert min_take_pct(cfg(min_take_cost_ratio=5), 'KRW', QUOTE) == pytest.approx(2.0)
    assert min_take_pct(cfg(), 'KRW', {'bid': 99.0, 'ask': 101.0}) == pytest.approx(3*2.4)
    assert min_take_pct(cfg(min_take_cost_ratio=0), 'KRW', QUOTE) == 0.0
    assert min_take_pct(SimpleNamespace(slippage_bps=5, fee_kr=15, fee_us=15, sell_tax_kr=0), 'KRW', QUOTE) == 0.0     # an older config object


def test_the_setting_has_a_safe_default_and_a_validated_range():
    assert Config().min_take_cost_ratio == 3.0
    make = lambda **over: Config(mode='demo', password='x'*8, session_secret='y'*32, **over)
    make().validate()
    make(min_take_cost_ratio=0).validate()
    make(min_take_cost_ratio=20).validate()
    for bad in (-1, 20.5, float('inf'), float('nan')):
        with pytest.raises(ValueError):
            make(min_take_cost_ratio=bad).validate()


# ---- the rule in the sizing -----------------------------------------------------------------------------------------------------

def sized(take, stop=.5, config=None, quote=QUOTE, symbol='005930', horizon='intraday', holding=60, **over):
    state = {'strategy_settings': {'horizon': horizon}, 'positions': over.pop('positions', {}), 'quotes': {}}
    constraints = {'portfolio_equity': 1_000_000, 'max_buy_quantity': 5000, 'max_sell_quantity': over.pop('max_sell', 0)}
    decision = {'stance': over.pop('stance', 'BUY'), 'target_weight_pct': over.pop('weight', 20), 'stop_loss_pct': stop,
                'take_profit_pct': take, 'max_holding_minutes': holding}
    return size_order(state, symbol, quote, decision, constraints, config or cfg(), now=NOW)


def test_a_buy_below_the_floor_is_refused_with_the_numbers():
    result = sized(1.19)
    assert result['quantity'] == 0
    assert '1.19%' in result['reason'] and '0.40%' in result['reason'] and '3배' in result['reason'] and '최소 1.20%' in result['reason']
    assert result['stop_price'] and result['take_profit_price']                       # the plan itself was valid; only the economics fail


def test_a_buy_exactly_at_the_floor_or_above_it_goes_ahead():
    assert sized(1.2)['quantity'] > 0                                                  # 3 x 0.40 exactly: not refused by a rounding accident
    assert sized(1.21)['quantity'] > 0 and sized(5.0, stop=2.0)['quantity'] > 0


def test_the_floor_moves_with_the_spread_the_tax_and_the_fee_of_the_currency():
    wide = {'bid': 99.5, 'ask': 100.5, 'session_end': NOW+7200}                       # a 1% spread: cost 1.4%, floor 4.2%
    assert sized(4.0, stop=1.0, quote=wide)['quantity'] == 0 and sized(4.2, stop=1.0, quote=wide)['quantity'] > 0
    taxed = cfg(sell_tax_kr=18)                                                        # cost 0.58%, floor 1.74%
    assert sized(1.7, config=taxed)['quantity'] == 0 and sized(1.74, config=taxed)['quantity'] > 0
    usd = cfg(fee_us=30)                                                               # cost 0.70%, floor 2.1% (USD); Korea stays 1.2%
    assert sized(2.0, config=usd, symbol='AAPL')['quantity'] == 0 and sized(2.1, config=usd, symbol='AAPL')['quantity'] > 0
    assert sized(1.2, config=usd)['quantity'] > 0


def test_the_ratio_can_be_tuned_or_switched_off():
    assert sized(.5, stop=.3, config=cfg(min_take_cost_ratio=0))['quantity'] > 0
    five = cfg(min_take_cost_ratio=5)
    assert sized(1.9, config=five)['quantity'] == 0 and sized(2.0, config=five)['quantity'] > 0


def test_a_sell_and_a_month_plan_are_not_held_to_it():
    held = {'005930': {'quantity': 10, 'average': 100.0, 'cost_basis': 1000.0}}
    sell = sized(.3, stance='SELL', weight=0, positions=held, max_sell=10)
    assert sell['quantity'] == 10                                                      # reducing a position owes nothing to a take-profit
    month = sized(3.0, stop=2.0, horizon='month', holding=20160)
    assert month['quantity'] > 0                                                       # a month plan's own minimum (3%) is above the floor


# ---- a conditional-entry plan below the floor is not kept -----------------------------------------------------------------------

def test_a_plan_below_the_floor_is_declined_with_the_reason():
    decision = {'stance': 'HOLD', 'target_weight_pct': 20, 'stop_loss_pct': .5, 'take_profit_pct': 1.0, 'max_holding_minutes': 60,
                'entry_type': 'breakout', 'entry_level': 101.0, 'entry_invalidate': 99.5, 'entry_minutes': 60}
    quote = {'bid': 100.0, 'ask': 100.1, 'session_end': NOW+10_000_000}
    fields, note = entry.plan_from(decision, quote=quote, horizon='intraday', now=NOW, currency='KRW', min_take_pct=1.2)
    assert fields is None and '최소 1.20%' in note and '1%' in note
    assert entry.plan_from(dict(decision, take_profit_pct=1.2), quote=quote, horizon='intraday', now=NOW, currency='KRW', min_take_pct=1.2)[0]
    assert entry.plan_from(decision, quote=quote, horizon='intraday', now=NOW, currency='KRW')[0]               # no floor given: as before


# ---- on a ledger: the analysis, the plan and what the AI is told ------------------------------------------------------------------

@pytest.fixture
def day(tmp_path):
    engine, store = build(tmp_path)
    yield engine
    store.release()


BUY = {'stance': 'BUY', 'quantity': 1, 'target_weight_pct': 20, 'max_holding_minutes': 60}


def test_an_analysed_buy_with_a_narrow_target_is_refused_and_the_log_says_why(day):
    day.script = dict(BUY, stop_loss_pct=.5, take_profit_pct=1.0)
    state = analyse(day)
    run = state['runs'][-1]
    assert state['trades'] == [] and state['positions'] == {}
    assert '왕복 비용' in run['blocked'] and '최소 1.20%' in run['blocked'] and run['sizing']['quantity'] == 0
    assert state['evaluations'][-1]['stance'] == 'BUY' and state['evaluations'][-1]['action'] == 'blocked'          # the AI's call is still scored
    assert any(e['level'] == 'warning' and '왕복 비용' in e['message'] for e in state['events'])


def test_the_same_buy_with_a_wide_enough_target_is_filled(day):
    day.script = dict(BUY, stop_loss_pct=.5, take_profit_pct=1.2)
    state = analyse(day)
    assert state['trades'] and state['trades'][-1]['side'] == 'BUY' and 'blocked' not in state['runs'][-1]


def test_the_director_is_told_the_cost_and_the_floor_before_it_decides(day):
    analyse(day)
    constraints = dict(day.contexts)['director']['constraints']
    assert constraints['round_trip_cost_pct'] == pytest.approx(.4) and constraints['min_take_profit_pct'] == pytest.approx(1.2)
    for text in (DESK_PROMPTS['director'], MONTH_PROMPTS['director']):
        assert 'round_trip_cost_pct' in text and 'min_take_profit_pct' in text
    assert all('min_take_profit_pct' not in DESK_PROMPTS[role] for role in ('planner', 'fundamental', 'technical', 'news', 'critic', 'selector'))


def test_a_plan_below_the_floor_is_not_kept_and_one_at_the_floor_is(day):
    state = watched(day, stop_loss_pct=.5, take_profit_pct=1.0)
    assert state['watches'] == [] and state['runs'][-1]['watch']['status'] == 'rejected' and '최소 1.20%' in state['runs'][-1]['watch']['note']
    state = watched(day, stop_loss_pct=.5, take_profit_pct=1.2)
    assert [w['status'] for w in state['watches']] == ['waiting']


def test_a_plan_is_checked_again_at_the_moment_it_trades(day, monkeypatch):
    watched(day, stop_loss_pct=.5, take_profit_pct=1.3)                              # fine while the book is tight ...
    at(day, LEVEL)
    poll(day)
    real = day.quote_for_trade
    monkeypatch.setattr(day, 'quote_for_trade', lambda symbol: dict(real(symbol), bid=real(symbol)['bid']*.99))      # ... a 1% spread opens up
    state = poll(day)
    [watch] = state['watches']
    assert watch['status'] == 'blocked' and '왕복 비용' in watch['outcome'] and state['trades'] == []


def test_the_dashboard_state_and_page_carry_the_rule(day):
    assert day.public_state()['config']['min_take_cost_ratio'] == 3.0
    assert "의 ${s.config.min_take_cost_ratio}배 미만인 매수는 서버가 거부합니다" in JS


def test_the_scoring_deducts_the_same_cost_estimate(day):
    quote = {'bid': 99.0, 'ask': 101.0}
    assert day.trade_cost_bps('005930', quote) == pytest.approx(round_trip_cost_pct(day.c, 'KRW', quote)*100)
    assert day.trade_cost_bps('005930', quote) == pytest.approx(2*15+2*5+200)
