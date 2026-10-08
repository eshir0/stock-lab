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

REANALYZE_SECONDS = 2*3600     # the same name is not analysed again inside this window (6 h until 2026-10-08: a name
                               # with a standing signal got one look a session) ...
REANALYZE_MOVE_PCT = 3.0       # ... unless its price moved this much since that analysis, or another rule fired
REASONS = {'signal': '규칙 신호', 'requested': '직접 요청', 'no_signal': '규칙 신호 없음',
           'recent': '최근 분석함', 'no_data': '일봉 부족', 'filtered': '연구로 제외한 매수 신호뿐', 'holding': '보유 중 · 청산 규칙이 관리'}
# Buy signals that no longer call the AI, per market and instrument type, from the pre-registered 2006-2026 research
# (tools/data/research_plan.json, Q1): dropped where the develop period (2006-2018) had a 95% interval entirely below
# zero AND the holdout (2019-2026) was negative too. Experiments choose it with strategy_settings.signal_filter.
SIGNAL_FILTERS = ('all', 'research', 'research_v2')
RESEARCH_DROPS = {('KR', False): ('golden_cross', 'momentum', 'breakout'), ('US', False): ('momentum', 'breakout'),
                  ('US', True): ('breakout',)}
# 'research_v2' (2026-10-07, tools/data/research_plan_us_signals.json): the same rule re-run for US names with the US exit
# of exit_profile=market_long (stop 3x ATR, take 3x, 63 sessions). Under that exit no US signal fails the rule (momentum
# and breakout turned positive in both periods), so US names drop nothing; Korean names keep Q1's drops.
RESEARCH_V2_DROPS = {('KR', False): ('golden_cross', 'momentum', 'breakout')}


def dropped(mode, market, etf):
    """The BUY rules this experiment ignores for a name of this market and type."""
    if mode == 'research_v2':
        return set(RESEARCH_V2_DROPS.get((market, bool(etf)), ()))
    return set(RESEARCH_DROPS.get((market, bool(etf)), ())) if mode == 'research' else set()


def wanted(signals, held):
    """Rules that point the way this position could act: SELL when held, BUY otherwise."""
    side = 'SELL' if held else 'BUY'
    return [name for name, value in (signals or {}).items() if value == side]


def assess(*, held, signals, last, price, now, forced=False, ignore=(), sell_signals=True):
    """{'eligible', 'reason', 'rules'} for one name. `last` is its previous evaluation record (or None). `ignore` lists BUY
    rules this experiment does not act on (RESEARCH_DROPS); sell signals are never ignored."""
    rules = wanted(signals, held)
    if held and not sell_signals and not forced:
        # research_plan_sell.json (2026-10-06): selling on a rule's SELL signal did worse than leaving held names to the
        # trailing stop and the holding limit in both markets and both periods, so those experiments do not ask the AI.
        return {'eligible': False, 'reason': 'holding', 'rules': []}
    filtered = [] if held else [r for r in rules if r in ignore]
    rules = [r for r in rules if r not in filtered]
    if forced:
        return {'eligible': True, 'reason': 'requested', 'rules': rules}
    if not rules:
        if filtered:
            return {'eligible': False, 'reason': 'filtered', 'rules': [], 'filtered': filtered}
        return {'eligible': False, 'reason': 'no_signal', 'rules': []}
    if last and now-last.get('time', 0) < REANALYZE_SECONDS:
        earlier = set(wanted(last.get('rules'), held))
        start = last.get('price')
        moved = abs(price/start-1)*100 if price and start else 0
        if set(rules) <= earlier and moved < REANALYZE_MOVE_PCT:
            return {'eligible': False, 'reason': 'recent', 'rules': rules}
    return {'eligible': True, 'reason': 'signal', 'rules': rules, **({'filtered': filtered} if filtered else {})}


def label(rules):
    return '·'.join(RULE_NAMES.get(r, r) for r in rules)


def summary_line(checks):
    """One readable line for the status bar and the event log."""
    parts = []
    for c in checks:
        text = REASONS.get(c['reason'], c['reason'])
        shown = c['rules'] if c['reason'] != 'filtered' else c.get('filtered') or []
        parts.append(f'{c["name"]} {text}' + (f'({label(shown)})' if shown and c['reason'] != 'no_signal' else ''))
    return ' · '.join(parts)
