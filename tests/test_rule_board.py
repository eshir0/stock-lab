"""The rule board: each rule's trigger price is exactly where the very same rule function starts to fire on the next bar,
the board marks ignored and fired rules, and the gate check carries it for the dashboard."""
import math
import random

import pytest

from app import rules


def series(seed, n=80, drift=0.0):
    random.seed(seed)
    price, out = 100.0, []
    for i in range(n):
        price *= math.exp(drift+random.gauss(0, .015))
        out.append({'time': i, 'open': price, 'high': price*1.01, 'low': price*.99, 'close': price, 'volume': 1000})
    return out


@pytest.mark.parametrize('seed', range(12))
@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_the_trigger_is_the_boundary_of_the_rule_itself(seed, side):
    candles = series(seed)
    for rule in rules.RULES:
        p = rules.trigger_price(candles, rule, side)
        if p is None:
            continue
        up = rules._DIRECTION[rule][side]
        fn = rules._FUNCTIONS[rule]
        at = fn(rules._with_close(candles, p))
        away = fn(rules._with_close(candles, p*(0.995 if up else 1.005)))
        assert at == side, (rule, side, p)
        last = candles[-1]['close']
        if (up and p > last*.41) or (not up and p < last*2.49):           # not clamped at the search edge
            assert away != side, (rule, side, p)


def test_breakout_triggers_just_above_the_last_twenty_highs():
    candles = series(3)
    expected = max(c['high'] for c in candles[-20:])
    assert rules.trigger_price(candles, 'breakout', 'BUY') == pytest.approx(expected, rel=1e-6)


def test_a_cross_that_already_happened_has_no_trigger():
    candles = series(5, drift=.01)                                  # a steady climb: SMA5 has been above SMA20 for days
    assert rules.golden_cross(candles) == 'HOLD' and rules.trigger_price(candles, 'golden_cross', 'BUY') is None


def test_the_board_lists_every_rule_with_the_side_that_matters_and_a_chart():
    candles = series(7)
    b = rules.board(candles, held=False, ignore={'momentum'})
    assert b['side'] == 'BUY' and [r['rule'] for r in b['rules']] == list(rules.RULES)
    assert next(r for r in b['rules'] if r['rule'] == 'momentum')['ignored']
    assert len(b['chart']['close']) == 60 and b['chart']['sma20'][-1] == pytest.approx(sum(c['close'] for c in candles[-20:])/20, rel=1e-4)
    held = rules.board(candles, held=True, ignore={'momentum'})
    assert held['side'] == 'SELL' and not any(r['ignored'] for r in held['rules'])  # sells are never filtered


def test_the_gate_check_carries_the_board(tmp_path, monkeypatch):
    from test_month_desk import desk as make, run_cycle    # noqa
    gen = make.__wrapped__(tmp_path)
    engine = next(gen)
    try:
        state = run_cycle(engine)
        checks = [c for c in state['desk_gate']['checked'] if c.get('board')]
        assert checks and all(len(c['board']['rules']) == 4 for c in checks)
        assert 'board' in engine.public_state()['desk_gate']['checked'][0]
    finally:
        next(gen, None)
