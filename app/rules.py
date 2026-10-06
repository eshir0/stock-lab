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


# ---- how far each rule is from firing (the dashboard's rule board) ------------------------------------------------------------
# For every rule the price at which the NEXT completed daily bar would make it fire, found by trying prices on the very same
# rule functions (so the board can never disagree with the gate). A live quote compared with these prices shows how close
# a name is. Nothing here trades or calls the AI.

# rule -> {side: True when a HIGHER close moves it towards firing}
_DIRECTION = {'golden_cross': {'BUY': True, 'SELL': False}, 'momentum': {'BUY': True, 'SELL': False},
              'mean_reversion': {'BUY': False, 'SELL': True}, 'breakout': {'BUY': True, 'SELL': False}}


def _with_close(candles, price):
    last = candles[-1]
    return candles+[{'open': price, 'high': price, 'low': price, 'close': price, 'volume': last.get('volume', 0)}]


def trigger_price(candles, rule, side):
    """The close of the next bar at which `rule` would say `side`, or None when no close within -60%..+150% would."""
    fn, up = _FUNCTIONS[rule], _DIRECTION[rule][side]
    last = candles[-1]['close']
    fires = lambda p: fn(_with_close(candles, p)) == side
    lo, hi = last*.4, last*2.5
    if up:
        if not fires(hi):
            return None
        if fires(lo):
            return lo
        for _ in range(60):
            mid = (lo+hi)/2
            lo, hi = (lo, mid) if fires(mid) else (mid, hi)
        return math.ceil(hi*1e4)/1e4                  # rounded TOWARDS firing, so the shown price really fires
    if not fires(lo):
        return None
    if fires(hi):
        return hi
    for _ in range(60):
        mid = (lo+hi)/2
        lo, hi = (mid, hi) if fires(mid) else (lo, mid)
    return math.floor(lo*1e4)/1e4


def board(candles, held, ignore=(), sells_off=False):
    """Per rule: the side that matters (SELL when held, BUY otherwise), whether it fired on the last completed bar, the
    trigger price for the next bar, and whether the experiment ignores it; plus 60 bars and the reference lines for a chart."""
    side = 'SELL' if held else 'BUY'
    now = signals(candles)
    rows = []
    for rule in RULES:
        rows.append({'rule': rule, 'side': side, 'fired': now.get(rule) == side,
                     'trigger': trigger_price(candles, rule, side) if len(candles) >= 23 else None,
                     'higher': _DIRECTION[rule][side], 'ignored': sells_off or ((not held) and rule in ignore)})
    closes = [c['close'] for c in candles]
    window = closes[-20:]
    mean = sum(window)/len(window)
    sigma = _std(window)
    tail = candles[-60:]
    sma20 = [round(_sma(closes[:len(closes)-len(tail)+i+1], 20), 4) if len(closes)-len(tail)+i+1 >= 20 else None
             for i in range(len(tail))]
    return {'side': side, 'rules': rows, 'last_close': closes[-1], 'last_time': candles[-1].get('time'),
            'chart': {'close': [round(c['close'], 4) for c in tail], 'sma20': sma20,
                      'high20': round(max(c['high'] for c in candles[-20:]), 4) if all('high' in c for c in candles[-20:]) else None,
                      'low20': round(min(c['low'] for c in candles[-20:]), 4) if all('low' in c for c in candles[-20:]) else None,
                      'band_low': round(mean-Z_REVERSION*sigma, 4), 'band_high': round(mean+Z_REVERSION*sigma, 4)}}
