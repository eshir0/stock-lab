"""Evidence packs from the history archive (tools/data/evidence.py, rebuilt every day after the close) for the AI desk.

The archive job writes one JSON file per catalogue name into a folder mounted read-only at EVIDENCE_DIR. This module
only reads them: a pack that is missing, unreadable, for another symbol or older than MAX_AGE days is treated as absent,
and the analysis goes on without it (the AI is told so). Nothing here can size or place an order.

`for_ai` puts the facts about the rules that started this analysis first; `summary` condenses them into a sign
('for' | 'against' | 'mixed') stored with the decision and the trade, so the scorecard can later compare decisions that
followed the evidence with those that went against it.
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

MAX_AGE = 4*86400
RULE_NAMES = {'golden_cross': '골든크로스', 'momentum': '모멘텀', 'mean_reversion': '평균회귀', 'breakout': '돌파'}


class EvidenceStore:
    def __init__(self, folder):
        self.folder = Path(folder) if folder else None
        self.cache = {}

    def get(self, symbol, now=None):
        """The pack for `symbol`, or None."""
        now = time.time() if now is None else now
        if not self.folder or not symbol or '/' in symbol or symbol.startswith('.'):
            return None
        path = self.folder/f'{symbol}.json'
        try:
            stamp = path.stat().st_mtime
            cached = self.cache.get(symbol)
            if cached and cached[0] == stamp:
                pack = cached[1]
            else:
                pack = json.loads(path.read_text(encoding='utf-8'))
                self.cache[symbol] = (stamp, pack)
        except (OSError, ValueError):
            return None
        if not isinstance(pack, dict) or pack.get('symbol') != symbol:
            return None
        try:
            built = datetime.fromisoformat(str(pack.get('built_at'))).timestamp()
        except ValueError:
            return None
        return pack if 0 <= now-built <= MAX_AGE else None


def _mean(block, *path):
    for key in path:
        if not isinstance(block, dict):
            return None
        block = block.get(key)
    return block if isinstance(block, (int, float)) and not isinstance(block, bool) else None


def triggers(pack, rules):
    """The facts about each rule that started this analysis, side by side."""
    out = []
    for rule in rules or []:
        group = (pack.get('group_base_rates') or {}).get(rule) or {}
        own = (pack.get('own_history') or {}).get(rule) or {}
        out.append({'rule': rule, 'name': RULE_NAMES.get(rule, rule),
                    'market_2006_2018_mean_pct': _mean(group, 'develop_2006_2018', 'mean_pct'),
                    'market_2006_2018_ci95_pct': (group.get('develop_2006_2018') or {}).get('ci95_pct'),
                    'market_2019_2026_mean_pct': _mean(group, 'holdout_2019_2026', 'mean_pct'),
                    'this_name_replay_mean_pct': _mean(own, 'replay_site_exit', 'mean_pct'),
                    'this_name_replay_trades': _mean(own, 'replay_site_exit', 'n'),
                    'this_name_forward_21d_mean_pct': _mean(own, 'forward_21d', 'mean_pct'),
                    'dropped_by_site_filter': bool(group.get('dropped_by_site_filter'))})
    return out


def summary(pack, rules):
    """{'sign', 'votes', 'as_of'}: how many of the archive's numbers point up or down for the triggering rules."""
    if not pack:
        return {'sign': 'none'}
    votes = []
    for t in triggers(pack, rules):
        votes += [t['market_2006_2018_mean_pct'], t['market_2019_2026_mean_pct'], t['this_name_replay_mean_pct']]
    analog = _mean(pack.get('analogs') or {}, 'forward_21d', 'mean_pct')
    if analog is not None and _mean(pack.get('analogs') or {}, 'forward_21d', 'n') and pack['analogs']['forward_21d']['n'] >= 10:
        votes.append(analog)
    votes = [v for v in votes if v is not None]
    up, down = sum(v > 0 for v in votes), sum(v < 0 for v in votes)
    sign = 'none' if not votes else 'for' if up > down else 'against' if down > up else 'mixed'
    return {'sign': sign, 'up': up, 'down': down, 'as_of': pack.get('as_of_bar')}


def for_ai(pack, rules):
    if not pack:
        return {'available': False, 'note': '오늘의 과거 근거 묶음이 없습니다(수집 중이거나 오래됨). 근거 묶음 없이 판단하세요.'}
    body = {k: v for k, v in pack.items() if k not in ('built_at',)}
    return {'available': True, 'triggered_rules': triggers(pack, rules), 'summary': summary(pack, rules), **body}


def brief(pack, rules):
    """A few numbers for the stock selector's candidate list."""
    if not pack:
        return None
    return {'summary': summary(pack, rules), 'triggered_rules': triggers(pack, rules),
            'analogs_21d': (pack.get('analogs') or {}).get('forward_21d'), 'now': pack.get('now')}
