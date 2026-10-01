"""Quota pacing: spend what an AI subscription window can still pay for evenly, instead of all at once.

A full analysis costs a sizeable share of a five-hour window (about 10-20% with Claude). At a 10-minute interval the desk
would use the window up in under an hour and then wait for hours. Pacing keeps the configured interval as the minimum and
stretches it only when the window could not pay for an analysis at that rate until the session ends or the window resets.
It changes WHEN the next analysis starts, never what is decided or traded.
"""
COST_DEFAULT = 15.0            # percent of a window one analysis uses, until the desk has measured it
COST_MIN, COST_MAX = 3.0, 40.0
PACE_MAX = 75*60               # the longest pause pacing will ask for, in seconds


SAMPLES = 9                    # recent readings the estimate is taken from


def learn(samples, before, after):
    """(recent readings, the cost to plan with): what one analysis used of the window, as the MEDIAN of the last SAMPLES
    readings. Not an average: other use of the same subscription while an analysis runs (an interactive session) inflates
    single readings, and one such reading must not double the plan. Readings that cannot be compared (missing, or the
    window reset in between) are skipped."""
    kept = [float(x) for x in (samples or []) if isinstance(x, (int, float)) and not isinstance(x, bool)][-SAMPLES:]
    if isinstance(before, (int, float)) and isinstance(after, (int, float)) and after > before:
        kept = (kept+[round(max(COST_MIN, min(COST_MAX, after-before)), 1)])[-SAMPLES:]
    if not kept:
        return kept, COST_DEFAULT
    ordered, mid = sorted(kept), len(kept)//2
    return kept, round(ordered[mid] if len(kept) % 2 else (ordered[mid-1]+ordered[mid])/2, 1)


def pace(base, *, pct, switch_pct, resets_at, until, now, cost, cap=PACE_MAX):
    """Seconds from the start of the running analysis to the next one: `base` or longer.

    `pct` is the window's use before this analysis, `switch_pct` the level at which the desk stops using this AI. The
    analyses still affordable after the running one are spread over the time to `until` (session end) or `resets_at`,
    whichever is sooner."""
    if pct is None or resets_at is None or until is None:
        return base
    cost = max(COST_MIN, cost)
    after = switch_pct-pct-cost                   # room left once the running analysis has been paid for
    horizon = min(resets_at, until)-now
    if after < cost or horizon <= 0:
        return base                               # nothing more is affordable (the usage gate pauses the desk) or no time left
    more = int(after//cost)
    return int(max(base, min(cap, horizon/(more+1))))
