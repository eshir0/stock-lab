"""What the AI subscriptions have left, and the rule for switching BEFORE a limit is hit.

The host bridge reports each provider's usage (Claude sends the real utilization of its 5-hour and weekly windows on
every call; Codex only says "exhausted until ..."). Before an analysis cycle starts, `plan()` picks the providers that are
usable now. A provider is skipped when it is exhausted or when any of its windows has reached `AI_SWITCH_AT_PCT`
(default 80%), so a cycle is not started on a provider that would run dry halfway through it. When nothing is usable the
cycle simply waits for the earliest reset instead of failing.

Nothing here can place or size an order; a wrong reading can only make the desk wait or pick another AI.
"""
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

LABELS = {'claude': 'Claude', 'codex': 'Codex', 'gemini': 'Gemini'}
WINDOW_LABELS = {'five_hour': '5시간', 'seven_day': '주간'}
CACHE_SECONDS = 20
ZONE = ZoneInfo('Asia/Seoul')


def clock_text(timestamp):
    return datetime.fromtimestamp(timestamp, ZONE).strftime('%m-%d %H:%M')


def window_pct(window, now):
    """Percent used. A window whose reset time has passed counts as empty: the reading is older than the reset."""
    reset = window.get('resets_at')
    if isinstance(reset, (int, float)) and reset <= now:
        return 0.0
    value = window.get('utilization')
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 0.0
    return round(max(0.0, min(1.0, float(value)))*100, 1)


class UsageGate:
    def __init__(self, config, fetch=None, clock=time.time):
        self.c, self.clock = config, clock
        self.lock = threading.Lock()
        self.cache = None                        # (fetched at, {provider: {'cooldown_until', 'limits'}})
        self.enabled = config.mode != 'demo'     # demo answers are scripted, so there is nothing to conserve
        self.fetch = fetch or (lambda: {})       # supplied by agents.py, the module that talks to the bridge

    def snapshot(self):
        now = self.clock()
        with self.lock:
            if self.cache and now-self.cache[0] < CACHE_SECONDS:
                return self.cache[1]
        fresh = self.fetch()
        with self.lock:
            # An unreachable bridge keeps the last reading and is not asked again for a few seconds.
            data = fresh if fresh is not None else (self.cache[1] if self.cache else {})
            self.cache = (now, data)
            return data

    def observe(self, provider, limits=None, cooldown_until=None):
        """Take a reading straight from a call's answer, so the next cycle sees it without waiting for the cache."""
        with self.lock:
            data = dict(self.cache[1]) if self.cache else {}
            entry = dict(data.get(provider) or {})
            if isinstance(limits, dict) and limits.get('windows'):
                entry['limits'] = limits
            if isinstance(cooldown_until, (int, float)) and cooldown_until > self.clock():
                entry['cooldown_until'] = cooldown_until
            data[provider] = entry
            self.cache = (self.clock(), data)

    def status(self, provider, data=None):
        now = self.clock()
        data = self.snapshot() if data is None else data
        entry = data.get(provider) or {}
        result = {'name': provider, 'label': LABELS.get(provider, provider), 'state': 'ok', 'pct': None,
                  'windows': {}, 'until': None, 'note': ''}
        cooldown = entry.get('cooldown_until')
        if isinstance(cooldown, (int, float)) and cooldown > now:
            return dict(result, state='exhausted', until=cooldown, note=f'사용량 소진 · {clock_text(cooldown)}까지')
        windows = {}
        for name, window in ((entry.get('limits') or {}).get('windows') or {}).items():
            if isinstance(window, dict):
                windows[name] = {'pct': window_pct(window, now), 'resets_at': window.get('resets_at')}
        if windows:
            result.update(windows=windows, pct=max(w['pct'] for w in windows.values()))
        blocking = {n: w for n, w in windows.items() if w['pct'] >= self.c.ai_switch_pct}
        if blocking:
            resets = [w['resets_at'] for w in blocking.values() if isinstance(w['resets_at'], (int, float))]
            until = max(resets) if resets else None
            worst = max(blocking, key=lambda n: blocking[n]['pct'])
            note = f'{WINDOW_LABELS.get(worst, worst)} 창 {blocking[worst]["pct"]:.0f}% (전환 기준 {self.c.ai_switch_pct:.0f}%)'
            return dict(result, state='high', until=until, note=note + (f' · {clock_text(until)} 초기화' if until else ''))
        return result

    def plan(self):
        """(usable providers in configured order, status of every provider, message for when none is usable)."""
        order = list(self.c.provider_order)
        if not self.enabled or not order:
            return order, [], ''
        data = self.snapshot()
        statuses = [self.status(p, data) for p in order]
        usable = [s['name'] for s in statuses if s['state'] == 'ok']
        if usable:
            return usable, statuses, ''
        resumes = [s['until'] for s in statuses if s['until']]
        message = 'AI 사용량 대기 · ' + ' · '.join(f'{s["label"]} {s["note"]}' for s in statuses)
        if resumes:
            message += f' · 가장 빠른 재개 {clock_text(min(resumes))}'
        return usable, statuses, message

    def summary(self):
        """What the dashboard shows. Cheap: one cached read."""
        if not self.enabled or not self.c.provider_order:
            return {'enabled': False, 'providers': []}
        usable, statuses, message = self.plan()
        return {'enabled': True, 'switch_pct': self.c.ai_switch_pct, 'active': usable[0] if usable else None,
                'usable': usable, 'message': message, 'providers': statuses}
