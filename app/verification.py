"""The verification plan: a bar fixed in advance that a strategy must clear before real money is even considered.

`start` fixes the plan when an experiment begins: how long to collect, how many closed trades and scored decisions, and
what "good enough" means. `judge` reads it on every dashboard refresh: while the samples are short the answer is
"collecting", then pass or fail, check by check. A fingerprint of everything that defines the strategy (prompts, rules,
limits, cost assumptions) is stored with the plan, so a change of strategy halfway through - which would mix two strategies
in one result - shows up instead of passing silently.

Paper trading only: passing switches nothing on; it only says a small real-money pilot is worth discussing.
"""
import hashlib
import json

from . import entry, gate, history, reuse, rules, universe
from .agents import desk_prompts
from .risk import BOUNDS, BREAKEVEN_PCT, TRAIL_ARM

STRATEGY_VERSION = 1       # raise when the strategy's logic changes in a way the fingerprint below cannot see
CRITERIA = {'min_days': 56, 'min_trades': 30, 'min_decisions': 100, 'decision_horizon': 'd5',
            'max_drawdown_pct': 5.0, 'drop_best_days': 3}
CONFIG_KEYS = ('fee_kr', 'fee_us', 'sell_tax_kr', 'slippage_bps', 'min_take_cost_ratio', 'research_reuse_seconds',
               'research_reuse_move_pct', 'conditional_entry', 'interval_seconds', 'focus_per_market', 'focus_ai',
               'fractional_us', 'providers', 'ai_light_roles', 'claude_model', 'claude_model_light', 'codex_model')


def fingerprint(config):
    """A short hash of what defines the month strategy. Bug fixes elsewhere leave it alone; a new prompt, rule parameter,
    limit or cost assumption changes it."""
    parts = {'version': STRATEGY_VERSION, 'prompts': desk_prompts('month'), 'bounds': BOUNDS['month'],
             'trail': [TRAIL_ARM, BREAKEVEN_PCT], 'gate': [gate.REANALYZE_SECONDS, gate.REANALYZE_MOVE_PCT],
             'rules': [rules.MOMENTUM_BARS, rules.Z_MOMENTUM, rules.Z_REVERSION],
             'history': [history.HISTORY_DAYS, history.FETCH_BARS], 'focus': universe.CAPS,
             'entry': [entry.MINUTES['month'], entry.MAX_AWAY_PCT['month'], entry.MAX_DEPTH_PCT, entry.CHASE_PCT,
                       entry.CONFIRM_POLLS, entry.VOLUME_RATIO, entry.MAX_WAITING],
             'reuse': [reuse.SPIKE_RATIO], 'config': {key: getattr(config, key, None) for key in CONFIG_KEYS}}
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def start(config, now):
    return {'started_at': now, 'version': STRATEGY_VERSION, 'fingerprint': fingerprint(config), 'criteria': dict(CRITERIA)}


def daily_changes(daily, initial):
    """Equity change of each recorded day: last value of the day minus the day before (the first day against the seed)."""
    out, previous = [], initial
    for day in sorted(daily):
        out.append(daily[day]-previous)
        previous = daily[day]
    return out


def _pct(value):
    return '—' if value is None else f'{value:+.2f}%'


def judge(state, *, tracking, report, evaluation, config, now):
    """The plan's verdict: 'collecting' | 'pass' | 'fail', the progress towards the sample it needs and every check. None
    for an experiment that started without a plan."""
    plan = state.get('verification')
    if not plan:
        return None
    c = {**CRITERIA, **(plan.get('criteria') or {})}
    performance, benchmark = state.get('performance') or {}, state.get('benchmark') or {}
    initial = state.get('initial') or {}
    active = [cy for cy in ('KRW', 'USD') if initial.get(cy, 0) > 0]
    scored = ((evaluation.get('horizons') or {}).get(c['decision_horizon']) or {})
    progress = [{'key': 'days', 'label': '검증 기간', 'value': round(max(0.0, now-plan['started_at'])/86400, 1),
                 'target': c['min_days'], 'unit': '일'},
                {'key': 'trades', 'label': '청산된 거래', 'value': report['closed'], 'target': c['min_trades'], 'unit': '건'},
                {'key': 'decisions', 'label': '채점된 판단(5거래일 뒤)', 'value': scored.get('scored', 0),
                 'target': c['min_decisions'], 'unit': '건'}]
    checks = []

    def add(key, label, ok, detail):
        checks.append({'key': key, 'label': label, 'ok': ok, 'detail': detail})

    ci, mean = report.get('ci_pct'), report.get('expectancy_pct')
    add('expectancy', '거래당 기대값 > 0, 95% 신뢰구간 하한도 > 0 (비용·세금 차감)',
        None if ci is None else bool(mean > 0 and ci[0] > 0),
        f'기대값 {_pct(mean)} · 신뢰구간 {_pct(ci[0])} ~ {_pct(ci[1])}' if ci else '청산된 거래 2건부터 계산합니다')
    for cy in active:
        mine, index = (performance.get(cy) or {}).get('return_pct'), benchmark.get(cy)
        add('benchmark_'+cy, f'{cy} 계좌 수익률 ≥ 같은 기간 {index["name"] if index else "지수 ETF"} 보유',
            None if index is None or mine is None else bool(mine >= index['return_pct']),
            f'이 실험 {_pct(mine)} · 지수 {_pct(index["return_pct"])}' if index else '지수 일봉을 아직 읽지 못했습니다')
    for cy in active:
        drawdown = (performance.get(cy) or {}).get('max_drawdown_pct')
        add('drawdown_'+cy, f'{cy} 최대 낙폭 ≤ {c["max_drawdown_pct"]:g}%',
            None if drawdown is None else bool(drawdown <= c['max_drawdown_pct']), f'최대 낙폭 {drawdown or 0:.2f}%')
    for cy in active:
        changes = daily_changes(((tracking.get(cy) or {}).get('daily') or {}), initial[cy])
        n = c['drop_best_days']
        rest = sum(changes)-sum(sorted(changes, reverse=True)[:n]) if len(changes) > n else None
        add('best_days_'+cy, f'{cy} 가장 좋았던 {n}일을 빼도 플러스', None if rest is None else bool(rest > 0),
            f'{len(changes)}일 기록' if rest is None else f'나머지 날의 손익 {rest:+,.2f}')
    groups = report.get('groups') or {}
    if (groups.get('leveraged') or {}).get('count'):
        plain = (groups.get('plain') or {}).get('expectancy_pct')
        add('without_leverage', '레버리지 ETF를 빼도 거래당 기대값 > 0', None if plain is None else bool(plain > 0),
            f'레버리지 제외 기대값 {_pct(plain)}')
    changed = plan.get('fingerprint') != fingerprint(config)
    ready = all(p['value'] >= p['target'] for p in progress)
    if not ready or any(ch['ok'] is None for ch in checks):
        status = 'fail' if ready and any(ch['ok'] is False for ch in checks) else 'collecting'
    else:
        status = 'pass' if all(ch['ok'] for ch in checks) else 'fail'
    return {'status': status, 'ready': ready, 'started_at': plan['started_at'], 'criteria': c, 'progress': progress,
            'checks': checks, 'strategy_changed': changed, 'version': plan.get('version'),
            'ai_vs_rule': scored.get('ai_vs_rule')}
