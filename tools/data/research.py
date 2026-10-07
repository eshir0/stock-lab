"""Pre-registered rule research (research_plan.json): Q1 signals, Q2 market regime, Q3 stop/take, Q4 holding, Q5 trailing.

Every variant trades the site's own rule signals (vectorised in backtest.signals, checked against app/rules.py) with the
exit chosen on 2026-10-03 (C: no sale at the target; the trailing stop or the holding limit ends the trade). Variants
change one question at a time from the baseline. Statistics are split into the develop period (2006-2018), where the
variants are compared, and the holdout (2019-2026), read only for the confirmation step. Bars are read conservatively,
real costs apply, prices are dividend-adjusted, and KR includes delisted names.

Usage: research.py [--workers 3]   -> DATA/results/research.json
"""
import argparse
import json
import math
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
from numba import njit

from backtest import COST, MIN_VALUE, signals

DATA = Path(os.environ.get('DATA', '/data'))
DEV_END = '2019-01-01'
START = '2006-01-01'
BASE = {'sig': 'all', 'regime': 'none', 'stop': 1.5, 'take': 1.5, 'hold': 21, 'arm': .5, 'trail': 1.0}
SIGNALS = {'all': ('golden_cross', 'momentum', 'mean_reversion', 'breakout'), 'golden_cross': ('golden_cross',),
           'momentum': ('momentum',), 'breakout': ('breakout',), 'mean_reversion': ('mean_reversion',),
           'trend': ('golden_cross', 'momentum', 'breakout')}
REGIMES = ('none', 'sma200', 'sma50', 'vix25', 'sma200_vix30')
INDEX = {'US': '^GSPC', 'KR': '^KS11'}
GROUPS = ('US stock', 'US ETF', 'KR stock', 'KR ETF')


def variants():
    out = {}

    def add(question, **change):
        v = {**BASE, **change}
        key = '|'.join(f'{k}={v[k]}' for k in BASE)
        out.setdefault(key, {'params': v, 'questions': []})['questions'].append(question)
    for s in SIGNALS:
        add('Q1', sig=s)
    for r in REGIMES:
        add('Q2', regime=r)
    for stop in (1.0, 1.5, 2.0, 3.0):
        for take in (1.5, 2.0, 3.0):
            add('Q3', stop=stop, take=take)
    for hold in (21, 42, 63):
        add('Q4', hold=hold)
    for arm in (.3, .5, .8, 1.0):
        for trail in (.75, 1.0, 1.5):
            add('Q5', arm=arm, trail=trail)
    return out


@njit
def simulate(o, h, l, c, ok, atr, stop_mult, take_mult, hold, arm, trail_mult, cost):
    n = len(c)
    ts = np.empty(n, np.int64)
    rets = np.empty(n, np.float64)
    helds = np.empty(n, np.int64)
    k = 0
    free = 0
    for t in range(60, n-2):
        if not ok[t] or t < free:
            continue
        e = t+1
        entry = o[e]
        sp = min(max(stop_mult*atr[t], .02), .15)
        tp = min(max(take_mult*sp, .03), .40)
        stop = entry*(1-sp)
        arm_at = entry+entry*tp*arm
        trail = sp*trail_mult
        high = entry
        last = min(e+hold, n)-1
        exitp = -1.0
        held = 0
        for i in range(e, last+1):
            if o[i] <= stop:
                exitp = o[i]
                held = i-e+1
                break
            if l[i] <= stop:
                exitp = stop
                held = i-e+1
                break
            if h[i] > high:
                high = h[i]
            if high >= arm_at:
                r = max(entry*1.003, high*(1-trail))
                if r > stop:
                    stop = r
        if exitp < 0:
            exitp = c[last]
            held = last-e+1
        ts[k] = t
        rets[k] = exitp/entry-1-cost
        helds[k] = held
        k += 1
        free = e+held
    return ts[:k], rets[:k], helds[:k]


def regime_masks(dates, market):
    """{regime: bool array} for these dates, from index and VIX closes known at each date's close."""
    def series(name):
        df = pd.read_parquet(DATA/'daily'/'MACRO'/f'{name}.parquet')[['date', 'close']].dropna()
        return df.set_index('date')['close'].sort_index()
    idx, vix = series(INDEX[market]), series('^VIX')
    if market == 'KR':
        vix = vix.shift(1)       # at the Korean close the US session of the same date has not happened yet
    frame = pd.DataFrame({'idx': idx, 'sma200': idx.rolling(200).mean(), 'sma50': idx.rolling(50).mean()})
    frame = frame.join(vix.rename('vix'), how='outer').sort_index().ffill()
    frame = frame.reindex(frame.index.union(pd.Index(dates))).ffill().reindex(pd.Index(dates))
    up200 = (frame['idx'] > frame['sma200']).to_numpy()
    up50 = (frame['idx'] > frame['sma50']).to_numpy()
    v = frame['vix'].to_numpy()
    return {'none': np.ones(len(dates), bool), 'sma200': up200, 'sma50': up50, 'vix25': v < 25,
            'sma200_vix30': up200 & (v < 30)}


def run_symbol(job):
    path, market, etf, specs = job
    try:
        df = pd.read_parquet(path).dropna(subset=['open', 'high', 'low', 'close'])
    except Exception:
        return []
    df = df[(df['close'] > 0) & (df['open'] > 0) & (df['low'] > 0)].reset_index(drop=True)
    if len(df) < 300:
        return []
    f = (df['adjclose']/df['close']).to_numpy() if 'adjclose' in df and df['adjclose'].notna().all() else np.ones(len(df))
    adj = pd.DataFrame({k: df[k]*f for k in ('open', 'high', 'low', 'close')})
    sig = signals(adj)
    tr = np.maximum(adj['high']-adj['low'], np.maximum(abs(adj['high']-adj['close'].shift(1)), abs(adj['low']-adj['close'].shift(1))))
    atr = (tr.rolling(14).mean()/adj['close']).to_numpy()
    value = pd.Series(df['close'].to_numpy()*df['volume'].fillna(0).to_numpy()).rolling(60).median().to_numpy()
    dates = df['date'].astype(str).to_numpy()
    c = adj['close'].to_numpy()
    jump = np.abs(np.diff(np.log(c), prepend=np.log(c[0]))) > .5
    # Bad prints are looked for only in what is known at the entry (the last 20 sessions and the signal day). A huge move
    # AFTER the entry stays in: it is what the trade would have lived through (2026-10-03 review: the old window looked
    # 64 sessions ahead and quietly dropped delisting crashes).
    clean = ~(pd.Series(jump).rolling(21, min_periods=1).max().astype(bool).to_numpy())
    base = (value >= MIN_VALUE[market]) & np.isfinite(atr) & (dates >= START) & clean
    regimes = regime_masks(dates, market)
    o, h, l = adj['open'].to_numpy(), adj['high'].to_numpy(), adj['low'].to_numpy()
    k = COST[market]
    cost = (2*k['fee']+2*k['slip']+(0 if etf else k['tax']))/1e4
    group = GROUPS.index(f'{market} {"ETF" if etf else "stock"}')
    out = []
    for key, p in specs.items():
        buy = sig[list(SIGNALS[p['sig']])].any(axis=1).to_numpy()
        ok = buy & base & regimes[p['regime']]
        ts, rets, helds = simulate(o, h, l, c, ok, atr, p['stop'], p['take'], int(p['hold']), p['arm'], p['trail'], cost)
        if len(ts):
            months = np.array([int(d[:4])*100+int(d[5:7]) for d in dates[ts]], np.int32)
            out.append((key, group, months, (rets*100).astype(np.float32), helds.astype(np.int16)))
    return out


def stats(months, rets):
    if len(rets) == 0:
        return {'n': 0}
    s = pd.Series(rets, dtype=float)
    by_month = s.groupby(months).mean()
    mean = float(s.mean())
    ci = None
    if len(by_month) >= 3:
        se = by_month.std(ddof=1)/math.sqrt(len(by_month))
        ci = [round(mean-1.96*se, 3), round(mean+1.96*se, 3)]
    return {'n': int(len(s)), 'mean': round(mean, 3), 'ci': ci, 'win': round(float((s > 0).mean()*100), 1)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--workers', type=int, default=3)
    p.add_argument('--us-wide-signals', action='store_true',
                   help='research_plan_us_signals.json: Q1 signal variants with the US exit (3x ATR, 3x, 63), US names only')
    a = p.parse_args()
    if a.us_wide_signals:
        global variants
        base_variants = variants

        def variants():
            out = {}
            for s in SIGNALS:
                v = {**BASE, 'sig': s, 'stop': 3.0, 'take': 3.0, 'hold': 63}
                out['|'.join(f'{k}={v[k]}' for k in BASE)] = {'params': v, 'questions': ['US-wide']}
            return out
    specs = {k: v['params'] for k, v in variants().items()}
    us = pd.read_parquet(DATA/'universe'/'us.parquet').set_index('yahoo')['etf'].to_dict()
    kr = pd.read_parquet(DATA/'universe'/'kr.parquet').set_index('yahoo')['etf'].to_dict()
    jobs = []
    folders = (('US', us, 'US'),) if a.us_wide_signals else (('US', us, 'US'), ('KR', kr, 'KR'), ('KR', {}, 'KR_DELISTED'))
    for market, etfs, folder in folders:
        for path in sorted((DATA/'daily'/folder).glob('*.parquet')):
            jobs.append((path, market, bool(etfs.get(path.stem, False)), specs))
    if os.environ.get('LIMIT'):
        jobs = jobs[::max(1, len(jobs)//int(os.environ['LIMIT']))]
    parts = {}
    with ProcessPoolExecutor(a.workers) as ex:
        for result in ex.map(run_symbol, jobs, chunksize=16):
            for key, group, months, rets, helds in result:
                parts.setdefault(key, []).append((group, months, rets, helds))
    dev_end = int(DEV_END[:4])*100+1
    plan_file = 'research_plan_us_signals.json' if a.us_wide_signals else 'research_plan.json'
    report = {'plan': json.loads((Path(__file__).parent/plan_file).read_text()), 'variants': {}}
    for key, info in variants().items():
        rows = parts.get(key, [])
        g = np.concatenate([np.full(len(m), gr, np.int8) for gr, m, _, _ in rows]) if rows else np.array([], np.int8)
        m = np.concatenate([m for _, m, _, _ in rows]) if rows else np.array([], np.int32)
        r = np.concatenate([x for _, _, x, _ in rows]) if rows else np.array([], np.float32)
        hd = np.concatenate([x for _, _, _, x in rows]) if rows else np.array([], np.int16)
        entry = {'params': info['params'], 'questions': info['questions'], 'avg_hold': round(float(hd.mean()), 1) if len(hd) else None}
        for period, mask in (('develop', m < dev_end), ('holdout', m >= dev_end)):
            entry[period] = {'all': stats(m[mask], r[mask])}
            for market in ('US', 'KR'):
                sel = mask & np.isin(g, [GROUPS.index(x) for x in GROUPS if x.startswith(market)])
                entry[period][market] = stats(m[sel], r[sel])
            for gi, name in enumerate(GROUPS):
                sel = mask & (g == gi)
                entry[period][name] = stats(m[sel], r[sel])
        report['variants'][key] = entry
    out = DATA/'results'
    out.mkdir(exist_ok=True)
    (out/('research-us-wide-signals.json' if a.us_wide_signals else 'research.json')).write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print('variants', len(report['variants']))


if __name__ == '__main__':
    main()
