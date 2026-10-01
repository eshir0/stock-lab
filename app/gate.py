"""The rule-signal gate: the AI is only asked about a name when a simple rule says there is something to look at.

An analysis cycle costs several AI calls, and on daily bars almost every cycle of a session would see the same picture.
So before any AI call the desk computes the four textbook rules (rules.py) on the completed daily bars of every candidate:

- a name that is not held is worth a look when at least one rule says BUY;
- a name that is held is worth a look when at least one rule says SELL (the stop, target, trailing stop, holding limit
  and list rotation exit positions without the AI either way);
- a name analysed in the last few hours is not looked at again unless its price moved a lot or a new rule fired;
- a name the owner asked for with "지금 분석" is always looked at.

When no candidate qualifies the cycle makes no AI call at all. Nothing here can place or size an order: a wrong answer
can only make the desk skip a look or take one.
"""
from .rules import RULE_NAMES

REANALYZE_SECONDS = 6*3600     # the same name is not analysed again inside this window ...
REANALYZE_MOVE_PCT = 3.0       # ... unless its price moved this much since that analysis, or another rule fired
REASONS = {'signal': '규칙 신호', 'requested': '직접 요청', 'no_signal': '규칙 신호 없음',
           'recent': '최근 분석함', 'no_data': '일봉 부족'}


def wanted(signals, held):
    """Rules that point the way this position could act: SELL when held, BUY otherwise."""
    side = 'SELL' if held else 'BUY'
    return [name for name, value in (signals or {}).items() if value == side]


def assess(*, held, signals, last, price, now, forced=False):
    """{'eligible', 'reason', 'rules'} for one name. `last` is its previous evaluation record (or None)."""
    rules = wanted(signals, held)
    if forced:
        return {'eligible': True, 'reason': 'requested', 'rules': rules}
    if not rules:
        return {'eligible': False, 'reason': 'no_signal', 'rules': []}
    if last and now-last.get('time', 0) < REANALYZE_SECONDS:
        earlier = set(wanted(last.get('rules'), held))
        start = last.get('price')
        moved = abs(price/start-1)*100 if price and start else 0
        if set(rules) <= earlier and moved < REANALYZE_MOVE_PCT:
            return {'eligible': False, 'reason': 'recent', 'rules': rules}
    return {'eligible': True, 'reason': 'signal', 'rules': rules}


def label(rules):
    return '·'.join(RULE_NAMES.get(r, r) for r in rules)


def summary_line(checks):
    """One readable line for the status bar and the event log."""
    parts = []
    for c in checks:
        text = REASONS.get(c['reason'], c['reason'])
        parts.append(f'{c["name"]} {text}' + (f'({label(c["rules"])})' if c['rules'] and c['reason'] != 'no_signal' else ''))
    return ' · '.join(parts)
