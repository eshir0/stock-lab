"""Research reuse: a second look at the same name soon after the first one does not repeat the slow research.

A full analysis is six AI calls, three of them research (the planner's task list, the company/product analyst and the news analyst; the
two that search the web are the heaviest). What those say about a company changes slowly, while the tape (technical analyst), the
objections (critic) and the decision (director) have to be fresh every time. So within a short time the earlier planner, fundamental and
news reports are reused and only technical + critic + director run: three calls instead of six.

Reuse is refused whenever what those reports describe may have changed: the research is too old, the price moved, volume surged (often
news), the earlier research had no sourced evidence, or the owner asked for this analysis. The reused copies keep the time and the price
of the ORIGINAL research, so reusing a reuse never keeps stale research alive.

Pure functions over plain dicts; the desk (`desk.py`) does the calls and the state.
"""
import copy

REUSED = ('planner', 'fundamental', 'news')
TECHNICAL = 'technical'
SPIKE_RATIO = 3.0                  # the last 5 minutes' volume this many times the 20 before: probably news, look it up again
SAVED_CALLS = len(REUSED)


def _anchor(run, reports):
    """The price the research was originally made at: a reuse carries it along, a fresh analysis made it at its own start."""
    carried = next((r['research_price'] for r in reports.values() if r.get('research_price')), None)
    return carried or run.get('price')


def find(state, symbol, *, price, spike, now, ttl, move_pct, horizon):
    """(reusable research, note). `research` is {'reports': {role: copy}, 'run_id', 'age_minutes', 'price'} for the latest completed
    analysis of `symbol`, or None; `note` explains the refusals worth showing (a move or a surge) and is '' for the quiet ones."""
    if ttl <= 0:
        return None, ''
    run = next((r for r in reversed(state.get('runs') or []) if r.get('symbol') == symbol and r.get('status') == 'completed'), None)
    if run is None or (run.get('horizon') or 'intraday') != horizon:
        return None, ''
    reports = {r.get('role'): r for r in run.get('reports') or [] if isinstance(r, dict) and r.get('role') in REUSED}
    if set(reports) != set(REUSED):
        return None, ''
    tasks = {t.get('role') for t in reports['planner'].get('tasks') or [] if isinstance(t, dict)}
    if not {TECHNICAL, 'fundamental', 'news'} <= tasks:
        return None, ''
    if any(not isinstance(r.get('time'), (int, float)) for r in reports.values()):
        return None, ''
    if now-min(r['time'] for r in reports.values()) > ttl:
        return None, ''
    if not any(reports[role].get('evidence') for role in ('fundamental', 'news')):
        return None, ''                                  # nothing sourced to build on: look again
    then = _anchor(run, reports)
    if (not isinstance(then, (int, float)) or isinstance(then, bool) or not then > 0
            or not isinstance(price, (int, float)) or not price > 0):
        return None, ''
    moved = abs(price/then-1)*100
    if moved >= move_pct:
        return None, f'가격이 조사 때보다 {moved:.1f}% 움직여 조사를 새로 합니다.'
    if spike is not None and spike >= SPIKE_RATIO:
        return None, f'거래량이 평소의 {spike:.1f}배로 늘어 조사를 새로 합니다.'
    out = {}
    for role in REUSED:
        report = copy.deepcopy(reports[role])
        report.pop('search_entry_point', None)            # display-only markup is not worth storing twice
        report.update(reused=True, age_minutes=int((now-report['time'])//60), research_price=then, usage={})
        out[role] = report
    return {'reports': out, 'run_id': run.get('id'), 'age_minutes': max(r['age_minutes'] for r in out.values()), 'price': then}, ''
