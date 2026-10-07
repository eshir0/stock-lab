"""Evidence packs from the history archive (tools/data/evidence.py, rebuilt every day after the close) for the AI desk.

The archive job writes one JSON file per catalogue name into a folder mounted read-only at EVIDENCE_DIR. This module
only reads them: a pack that is missing, unreadable, for another symbol or older than MAX_AGE days is treated as absent,
and the analysis goes on without it (the AI is told so). Nothing here can size or place an order.

`for_ai` puts the facts about the rules that started this analysis first; `summary` condenses them into a sign
('for' | 'against' | 'mixed', near-zero numbers counted as flat) stored with the decision and the trade, so the scorecard can later compare decisions that
followed the evidence with those that went against it.
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

MAX_AGE = 4*86400
# The prices under a pack must be recent too: a pack rebuilt today from a price file that stopped updating is not fresh.
# 8 calendar days covers a weekend plus the longest Korean holiday run (Chuseok/Lunar New Year with substitutes).
MAX_BAR_AGE = 8*86400
RULE_NAMES = {'golden_cross': '골든크로스', 'momentum': '모멘텀', 'mean_reversion': '평균회귀', 'breakout': '돌파'}


class EvidenceStore:
    def __init__(self, folder):
        self.folder = Path(folder) if folder else None

    def get(self, symbol, now=None):
        """The pack for `symbol`, or None."""
        now = time.time() if now is None else now
        if not self.folder or not symbol or '/' in symbol or symbol.startswith('.'):
            return None
        path = self.folder/f'{symbol}.json'
        try:
            pack = json.loads(path.read_text(encoding='utf-8'))      # ~4 KB, read fresh each time: no stale cache
        except (OSError, ValueError):
            return None
        if not isinstance(pack, dict) or pack.get('symbol') != symbol:
            return None
        try:
            built = datetime.fromisoformat(str(pack.get('built_at'))).timestamp()
        except ValueError:
            return None
        if not 0 <= now-built <= MAX_AGE or pack.get('stale'):
            return None
        try:
            bar = datetime.fromisoformat(str(pack.get('as_of_bar'))).timestamp()
        except ValueError:
            return None
        return pack if now-bar <= MAX_BAR_AGE else None


def _mean(block, *path):
    for key in path:
        if not isinstance(block, dict):
            return None
        block = block.get(key)
    return block if isinstance(block, (int, float)) and not isinstance(block, bool) else None


EXITS = {'base': '추적 손절 · 손절 1.5×ATR · 최대 21거래일', 'us_long': '추적 손절 · 손절 3×ATR · 최대 63거래일'}


def exit_for(settings, market):
    """Which archived exit matches how this experiment trades a name: US names of exit_profile=market_long trade with the
    wide stop and 63 sessions ('us_long'); everything else with the base exit the packs were first built on."""
    return 'us_long' if market == 'US' and (settings or {}).get('exit_profile') == 'market_long' else 'base'


def triggers(pack, rules, exit='base'):
    """The facts about each rule that started this analysis, side by side, for the exit this experiment uses (a pack
    built before the long-exit numbers existed falls back to the base exit and says so)."""
    out = []
    for rule in rules or []:
        group = (pack.get('group_base_rates') or {}).get(rule) or {}
        own = (pack.get('own_history') or {}).get(rule) or {}
        used = 'base'
        if exit == 'us_long' and group.get('us_long') and own.get('replay_us_long'):
            group, own, used = dict(group['us_long'], dropped_by_site_filter=False), dict(own, replay_site_exit=own['replay_us_long']), 'us_long'
        out.append({'rule': rule, 'name': RULE_NAMES.get(rule, rule), 'exit': EXITS[used],
                    'market_2006_2018_mean_pct': _mean(group, 'develop_2006_2018', 'mean_pct'),
                    'market_2006_2018_ci95_pct': (group.get('develop_2006_2018') or {}).get('ci95_pct'),
                    'market_2019_2026_mean_pct': _mean(group, 'holdout_2019_2026', 'mean_pct'),
                    'this_name_replay_mean_pct': _mean(own, 'replay_site_exit', 'mean_pct'),
                    'this_name_replay_trades': _mean(own, 'replay_site_exit', 'n'),
                    'this_name_forward_21d_mean_pct': _mean(own, 'forward_21d', 'mean_pct'),
                    'dropped_by_site_filter': bool(group.get('dropped_by_site_filter'))})
    return out


FLAT_PCT = .1           # a per-trade mean after costs closer to zero than this is a draw, not a vote either way
FLAT_ANALOG_PCT = .5    # the same for the analogs' 21-day excess over the name's usual 21 days (a noisier number)


def summary(pack, rules, exit='base'):
    """{'sign', 'up', 'down', 'flat', 'as_of'}: how many of the archive's numbers point up or down for the triggering rules.

    Each rule gives three votes (the market's 2006-2018 and 2019-2026 means, this name's replay), all per trade after
    costs; a mean within FLAT_PCT of zero counts as flat (2026-10-06: +0.03% used to outvote a significant -0.38%). The
    analogs vote only by how far their 21-day result beats or trails the name's usual 21 days in the same regime - the raw
    number is positive for any name that rose over the years - and only with a baseline and at least 10 cases."""
    if not pack:
        return {'sign': 'none'}
    votes = []
    for t in triggers(pack, rules, exit):
        votes += [(t[k], FLAT_PCT) for k in ('market_2006_2018_mean_pct', 'market_2019_2026_mean_pct', 'this_name_replay_mean_pct')]
    analogs = pack.get('analogs') or {}
    analog, usual = _mean(analogs, 'forward_21d', 'mean_pct'), _mean(analogs, 'baseline_forward_21d', 'mean_pct')
    if analog is not None and usual is not None and (_mean(analogs, 'forward_21d', 'n') or 0) >= 10:
        votes.append((analog-usual, FLAT_ANALOG_PCT))
    votes = [(v, band) for v, band in votes if v is not None]
    up, down = sum(v >= band for v, band in votes), sum(v <= -band for v, band in votes)
    sign = 'none' if not votes else 'for' if up > down else 'against' if down > up else 'mixed'
    return {'sign': sign, 'up': up, 'down': down, 'flat': len(votes)-up-down, 'as_of': pack.get('as_of_bar')}


def for_ai(pack, rules, exit='base'):
    if not pack:
        return {'available': False, 'note': '오늘의 과거 근거 묶음이 없습니다(수집 중이거나 오래됨). 근거 묶음 없이 판단하세요.'}
    body = {k: v for k, v in pack.items() if k not in ('built_at', 'series')}     # chart series are for the dashboard only
    return {'available': True, 'exit_used': EXITS['us_long' if exit == 'us_long' else 'base'],
            'triggered_rules': triggers(pack, rules, exit), 'summary': summary(pack, rules, exit), **body}


def brief(pack, rules, exit='base'):
    """A few numbers for the stock selector's candidate list."""
    if not pack:
        return None
    return {'summary': summary(pack, rules, exit), 'triggered_rules': triggers(pack, rules, exit),
            'analogs_21d': (pack.get('analogs') or {}).get('forward_21d'), 'now': pack.get('now'),
            'long_term': {k: (pack.get('long_term') or {}).get(k) for k in ('ret_1y_pct', 'ret_5y_pct', 'position_in_52w_pct',
                                                                     'from_all_time_high_pct', 'max_drawdown_pct')}}
