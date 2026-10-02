"""Exit-rule backtest on the history archive: A (sell all at the target - the site's rule), B (sell half at the target, trail
the rest), C (no target: trail the high), C3 (C with a three-month limit instead of one).

Entries are the site's own rule signals (app/rules.py, vectorised here and checked bar-for-bar against it): a BUY from any
rule at a daily close buys at the next open. This is the "AI 없이 규칙대로" baseline - the AI's own picks cannot be replayed
on the past, because the model already knows what happened. Every variant trades the very same entries, so the comparison
between exits is paired.

The position plan imitates what the desk does: stop = 1.5 x the 14-day average range (2-15%), target = 1.5 x the stop (3-40%),
the stop rises once the price is half way to the target (first to entry + 0.3%, then the high minus the stop distance), and
the position ends after 21 sessions (63 for C3). Bars are read conservatively: a gap below the stop sells at the open, a bar
that touches both the stop and the target counts as a stop, and a bar's high only moves the stop for the next bar.
Costs are the site's real schedule (KR 1.5 bp + 20 bp tax on stocks, US 10 bp, 5 bp slippage each side). Prices are
dividend-adjusted (total return).

Usage: backtest.py [--start 2006-01-01] [--workers 3] [--pool-only]
"""
import argparse
import json
import math
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(os.environ.get('DATA', '/data'))
RULE_STYLE_TREND = ('golden_cross', 'momentum', 'breakout')
VARIANTS = ('A', 'B', 'C', 'C3')
HOLD = {'A': 21, 'B': 21, 'C': 21, 'C3': 63}
COST = {'KR': {'fee': 1.5, 'tax': 20.0, 'slip': 5.0}, 'US': {'fee': 10.0, 'tax': 0.0, 'slip': 5.0}}
MIN_VALUE = {'KR': 5e9, 'US': 2e7}           # median daily traded value over 60 sessions, known at the entry
ATR_MULT, TAKE_MULT, ARM, BREAKEVEN = 1.5, 1.5, .5, .003


# ---- the site's rule signals, vectorised --------------------------------------------------------------------------------------

def signals(df):
    c, h = df['close'], df['high']
    sma5, sma20 = c.rolling(5).mean(), c.rolling(20).mean()
    d = sma5-sma20
    prior_nonpos = (d.shift(1) <= 0) | (d.shift(2) <= 0) | (d.shift(3) <= 0)
    golden = (d > 0) & prior_nonpos & (np.arange(len(c)) >= 22)
    steps = np.log(c/c.shift(1))
    sigma = steps.rolling(20).std(ddof=0)
    z_mom = np.log(c/c.shift(20))/(sigma*math.sqrt(20))
    momentum = (z_mom >= 1.0) & (sigma > 0)
    std20 = c.rolling(20).std(ddof=0)
    z_rev = (c-sma20)/std20
    reversion = (z_rev <= -2.0) & (std20 > 0)
    breakout = (c > h.shift(1).rolling(20).max()) & (np.arange(len(c)) >= 20)
    return pd.DataFrame({'golden_cross': golden, 'momentum': momentum, 'mean_reversion': reversion, 'breakout': breakout},
                        index=df.index).fillna(False)


def style(row):
    trend = row['golden_cross'] or row['momentum'] or row['breakout']
    rev = row['mean_reversion']
    return 'mixed' if trend and rev else 'trend' if trend else 'range'


# ---- one trade, four exits ----------------------------------------------------------------------------------------------------

def run_exit(variant, o, h, l, c, start, entry, stop_pct, take_pct):
    """(gross return as a fraction of the entry, sessions held) for one exit variant, starting at bar `start` (bought at its
    open). Half sales (B) are averaged."""
    stop = entry*(1-stop_pct)
    take = entry*(1+take_pct)
    trail = stop_pct
    arm_at = entry+(take-entry)*ARM
    high = entry
    parts, left = [], 1.0
    last = min(start+HOLD[variant], len(c))-1
    for i in range(start, last+1):
        if o[i] <= stop:
            parts.append((left, o[i])); left = 0
        elif l[i] <= stop:
            parts.append((left, stop)); left = 0
        elif h[i] >= take and variant in ('A', 'B') and (variant == 'A' or left == 1.0):
            if variant == 'A':
                parts.append((1.0, take)); left = 0
            else:
                parts.append((.5, take)); left = .5
                stop = max(stop, entry*(1+BREAKEVEN))
        if left == 0:
            return sum(w*p for w, p in parts)/entry-1, i-start+1
        high = max(high, h[i])
        if high >= arm_at:
            raised = max(stop, entry*(1+BREAKEVEN), high*(1-trail))
            if variant == 'A':
                raised = min(raised, take*.9999)
            stop = max(stop, raised)
    parts.append((left, c[last]))
    return sum(w*p for w, p in parts)/entry-1, last-start+1


def cost_of(market, etf):
    k = COST[market]
    return (2*k['fee']+2*k['slip']+(0 if etf else k['tax']))/1e4


def simulate(path, market, etf, start_date, pool):
    try:
        df = pd.read_parquet(path)
    except Exception:
        return []
    df = df.dropna(subset=['open', 'high', 'low', 'close'])
    df = df[(df['close'] > 0) & (df['open'] > 0) & (df['low'] > 0)]
    if len(df) < 300:
        return []
    if 'adjclose' in df and df['adjclose'].notna().all():
        f = (df['adjclose']/df['close']).to_numpy()
    else:
        f = np.ones(len(df))
    raw_close, volume = df['close'].to_numpy(), df['volume'].fillna(0).to_numpy()
    adj = pd.DataFrame({'open': df['open']*f, 'high': df['high']*f, 'low': df['low']*f, 'close': df['close']*f})
    adj.index = range(len(adj))
    sig = signals(adj)
    tr = np.maximum(adj['high']-adj['low'], np.maximum(abs(adj['high']-adj['close'].shift(1)), abs(adj['low']-adj['close'].shift(1))))
    atr_pct = (tr.rolling(14).mean()/adj['close']).to_numpy()
    value = pd.Series(raw_close*volume).rolling(60).median().to_numpy()
    dates = df['date'].to_numpy()
    o, h, l, c = (adj[k].to_numpy() for k in ('open', 'high', 'low', 'close'))
    # Bad prints: a day that moves more than 60% with no split recorded is skipped as an entry and ends the scan of a trade.
    jumps = np.abs(np.diff(np.log(c), prepend=np.log(c[0]))) > .5
    buy = sig.any(axis=1).to_numpy()
    cost = cost_of(market, etf)
    out, free_at = [], 0
    symbol = path.stem
    for t in range(60, len(c)-2):
        if not buy[t] or t < free_at or dates[t] < start_date:
            continue
        if not pool and not (value[t] >= MIN_VALUE[market]):
            continue
        if not np.isfinite(atr_pct[t]) or jumps[max(0, t-20):t+64].any():
            continue
        entry_i = t+1
        entry = o[entry_i]
        stop_pct = min(max(ATR_MULT*atr_pct[t], .02), .15)
        take_pct = min(max(TAKE_MULT*stop_pct, .03), .40)
        row = sig.iloc[t]
        rec = {'symbol': symbol, 'market': market, 'etf': bool(etf), 'date': dates[t], 'style': style(row),
               'rules': [k for k in sig.columns if row[k]], 'stop_pct': round(stop_pct*100, 2), 'take_pct': round(take_pct*100, 2)}
        longest = 0
        for v in VARIANTS:
            gross, held = run_exit(v, o, h, l, c, entry_i, entry, stop_pct, take_pct)
            rec[v] = round((gross-cost)*100, 4)
            rec[v+'_days'] = held
            longest = max(longest, held) if v != 'C3' else longest
        out.append(rec)
        free_at = entry_i+rec['A_days']          # the site holds one position per name: the next entry waits for the exit
    return out


# ---- statistics ---------------------------------------------------------------------------------------------------------------

def month_ci(df, col):
    """Mean per trade and a 95% interval that treats each entry month as one draw (trades in the same month move together)."""
    if df.empty:
        return None, None, None
    by_month = df.groupby(df['date'].str[:7])[col].mean()
    n = len(by_month)
    mean = df[col].mean()
    if n < 3:
        return round(mean, 3), None, None
    se = by_month.std(ddof=1)/math.sqrt(n)
    return round(mean, 3), round(mean-1.96*se, 3), round(mean+1.96*se, 3)


def describe(df):
    rows = []
    for v in VARIANTS:
        r = df[v]
        mean, lo, hi = month_ci(df, v)
        wins, losses = r[r > 0], r[r <= 0]
        rows.append({'variant': v, 'trades': int(len(r)), 'win_rate': round(float((r > 0).mean()*100), 1) if len(r) else None,
                     'mean_pct': mean, 'ci': [lo, hi], 'median_pct': round(float(r.median()), 3) if len(r) else None,
                     'profit_factor': round(float(wins.sum()/-losses.sum()), 3) if len(losses) and losses.sum() < 0 else None,
                     'avg_days': round(float(df[v+'_days'].mean()), 1) if len(r) else None,
                     'best_pct': round(float(r.max()), 2) if len(r) else None})
    diffs = {}
    for v in ('B', 'C', 'C3'):
        tmp = df.assign(diff=df[v]-df['A'])
        mean, lo, hi = month_ci(tmp, 'diff')
        diffs[v+'-A'] = {'mean_pct': mean, 'ci': [lo, hi], 'better_share': round(float((tmp['diff'] > 0).mean()*100), 1)}
    return {'variants': rows, 'paired': diffs}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--start', default='2006-01-01')
    p.add_argument('--workers', type=int, default=3)
    p.add_argument('--pool-only', action='store_true')
    a = p.parse_args()
    sys.path.insert(0, '/app')
    from app.instruments import SYMBOLS                     # the site's 36-name pool
    pool = {(s if v['market'] == 'US' else s+'.KS') for s, v in SYMBOLS.items()}
    us = pd.read_parquet(DATA/'universe'/'us.parquet').set_index('yahoo')['etf'].to_dict()
    kr = pd.read_parquet(DATA/'universe'/'kr.parquet').set_index('yahoo')['etf'].to_dict()
    jobs = []
    for market, etfs in (('US', us), ('KR', kr)):
        for path in sorted((DATA/'daily'/market).glob('*.parquet')):
            in_pool = path.stem in pool or path.stem.replace('.KQ', '.KS') in pool
            if a.pool_only and not in_pool:
                continue
            jobs.append((path, market, bool(etfs.get(path.stem, False)), a.start, in_pool))
    trades = []
    with ProcessPoolExecutor(a.workers) as ex:
        for i, result in enumerate(ex.map(simulate_job, jobs, chunksize=20)):
            trades.extend(result)
    df = pd.DataFrame(trades)
    out = DATA/'results'
    out.mkdir(exist_ok=True)
    tag = 'pool' if a.pool_only else 'all'
    df.to_parquet(out/f'trades-{tag}.parquet', index=False)
    report = {'tag': tag, 'start': a.start, 'names': len(jobs), 'trades': len(df), 'cost_bp': COST,
              'overall': describe(df)}
    for key in ('market', 'style'):
        report['by_'+key] = {k: describe(g) for k, g in df.groupby(key)}
    report['by_market_style'] = {f'{m}/{s}': describe(g) for (m, s), g in df.groupby(['market', 'style'])}
    df['period'] = np.where(df['date'] < '2016-01-01', '2006-2015', '2016-2026')
    report['by_period'] = {k: describe(g) for k, g in df.groupby('period')}
    (out/f'report-{tag}.json').write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    print(json.dumps(report['overall'], ensure_ascii=False, indent=1))


def simulate_job(job):
    return simulate(*job)


if __name__ == '__main__':
    main()
