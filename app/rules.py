"""Transparent rule-based baselines the AI's calls are scored against.

Textbook definitions on completed 1-minute candles with unfitted default thresholds. They exist to
answer one question: does the AI beat simple mechanical rules at the same moments and costs?
Comparing against preset strategies is an idea taken from Korea Investment & Securities' public
sample repository; no code from it is used.
"""
import math

RULES = ('golden_cross', 'momentum', 'mean_reversion', 'breakout')
RULE_NAMES = {'golden_cross': '골든크로스', 'momentum': '모멘텀', 'mean_reversion': '평균회귀', 'breakout': '돌파'}
MOMENTUM_BARS = 20
Z_MOMENTUM = 1.0
Z_REVERSION = 2.0


def _sma(values, n):
    return sum(values[-n:])/n


def _std(values):
    mean = sum(values)/len(values)
    return math.sqrt(sum((x-mean)**2 for x in values)/len(values))


def golden_cross(candles):
    """SMA5 crossing SMA20 within the last three bars: up = BUY, down = SELL."""
    closes = [c['close'] for c in candles]
    if len(closes) < 23:
        return None
    diffs = [_sma(closes[:len(closes)-3+j], 5)-_sma(closes[:len(closes)-3+j], 20) for j in range(4)]
    if diffs[3] > 0 and any(d <= 0 for d in diffs[:3]):
        return 'BUY'
    if diffs[3] < 0 and any(d >= 0 for d in diffs[:3]):
        return 'SELL'
    return 'HOLD'


def momentum(candles):
    """20-bar return measured in units of its own noise (volatility-scaled, so any price level works)."""
    closes = [c['close'] for c in candles]
    if len(closes) < MOMENTUM_BARS+1 or min(closes[-MOMENTUM_BARS-1:]) <= 0:
        return None
    window = closes[-MOMENTUM_BARS-1:]
    steps = [math.log(b/a) for a, b in zip(window, window[1:])]
    sigma = _std(steps)
    if sigma <= 0:
        return None
    z = math.log(window[-1]/window[0])/(sigma*math.sqrt(MOMENTUM_BARS))
    return 'BUY' if z >= Z_MOMENTUM else 'SELL' if z <= -Z_MOMENTUM else 'HOLD'


def mean_reversion(candles):
    """Price far below its 20-bar mean is bought, far above is sold (fade the stretch)."""
    closes = [c['close'] for c in candles]
    if len(closes) < 20:
        return None
    window = closes[-20:]
    sigma = _std(window)
    if sigma <= 0:
        return 'HOLD'
    z = (window[-1]-sum(window)/len(window))/sigma
    return 'BUY' if z <= -Z_REVERSION else 'SELL' if z >= Z_REVERSION else 'HOLD'


def breakout(candles):
    """Close beyond the prior 20 bars' high (BUY) or low (SELL)."""
    if len(candles) < 21 or any('high' not in c or 'low' not in c for c in candles[-21:]):
        return None
    prior = candles[-21:-1]
    close = candles[-1]['close']
    if close > max(c['high'] for c in prior):
        return 'BUY'
    if close < min(c['low'] for c in prior):
        return 'SELL'
    return 'HOLD'


_FUNCTIONS = {'golden_cross': golden_cross, 'momentum': momentum,
              'mean_reversion': mean_reversion, 'breakout': breakout}


def signals(candles):
    """{rule: 'BUY' | 'SELL' | 'HOLD' | None}; None means the rule cannot be computed from this data."""
    result = {}
    for name in RULES:
        try:
            result[name] = _FUNCTIONS[name](candles)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            result[name] = None
    return result
