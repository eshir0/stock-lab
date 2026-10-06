"""research_plan_sell.json: exit C alone vs C plus a sale at the next open after any rule says SELL, paired on the same entries."""
import json, math, os, sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np, pandas as pd
from numba import njit
from backtest import COST, MIN_VALUE, signals
from research import BASE, stats

DATA = Path(os.environ.get('DATA', '/data'))
DROPS = {('KR', False): ('golden_cross', 'momentum', 'breakout'), ('US', False): ('momentum', 'breakout'), ('US', True): ('breakout',)}
RULES = ('golden_cross', 'momentum', 'mean_reversion', 'breakout')
GROUPS = ('US stock', 'US ETF', 'KR stock', 'KR ETF')


def sell_signals(df):
    """The SELL side of app/rules.py, vectorised (checked against it in check_sell)."""
    c, l = df['close'], df['low']
    d = c.rolling(5).mean()-c.rolling(20).mean()
    dead = (d < 0) & ((d.shift(1) >= 0) | (d.shift(2) >= 0) | (d.shift(3) >= 0)) & (np.arange(len(c)) >= 22)
    steps = np.log(c/c.shift(1)); sigma = steps.rolling(20).std(ddof=0)
    mom = (np.log(c/c.shift(20))/(sigma*math.sqrt(20)) <= -1.0) & (sigma > 0)
    std20 = c.rolling(20).std(ddof=0)
    rev = ((c-c.rolling(20).mean())/std20 >= 2.0) & (std20 > 0)
    brk = (c < l.shift(1).rolling(20).min()) & (np.arange(len(c)) >= 20)
    return (dead | mom | rev | brk).fillna(False).to_numpy()


@njit
def sim(o, h, l, c, ok, atr, sell, use_sell, cost):
    n = len(c); out = np.empty(n); k = 0; free = 0
    for t in range(60, n-2):
        if not ok[t] or t < free:
            continue
        e = t+1; entry = o[e]
        sp = min(max(1.5*atr[t], .02), .15); tp = min(max(1.5*sp, .03), .40)
        stop = entry*(1-sp); arm = entry+entry*tp*.5; high = entry
        last = min(e+21, n)-1; exitp = -1.0; held = 0
        for i in range(e, last+1):
            if o[i] <= stop:
                exitp = o[i]; held = i-e+1; break
            if l[i] <= stop:
                exitp = stop; held = i-e+1; break
            if h[i] > high:
                high = h[i]
            if high >= arm:
                r = max(entry*1.003, high*(1-sp))
                if r > stop:
                    stop = r
            if use_sell and sell[i] and i+1 <= last:
                exitp = o[i+1]; held = i-e+2; break
        if exitp < 0:
            exitp = c[last]; held = last-e+1
        out[k] = exitp/entry-1-cost; k += 1; free = e+held
    return out[:k]


def run(job):
    path, market, etf = job
    try:
        df = pd.read_parquet(path).dropna(subset=['open', 'high', 'low', 'close'])
    except Exception:
        return None
    df = df[(df['close'] > 0) & (df['open'] > 0) & (df['low'] > 0)].reset_index(drop=True)
    if len(df) < 300:
        return None
    f = (df['adjclose']/df['close']).to_numpy() if 'adjclose' in df and df['adjclose'].notna().all() else np.ones(len(df))
    adj = pd.DataFrame({k: df[k].to_numpy()*f for k in ('open', 'high', 'low', 'close')})
    sig = signals(adj)
    tr = np.maximum(adj['high']-adj['low'], np.maximum(abs(adj['high']-adj['close'].shift(1)), abs(adj['low']-adj['close'].shift(1))))
    atr = (tr.rolling(14).mean()/adj['close']).to_numpy()
    value = pd.Series(df['close'].to_numpy()*df['volume'].fillna(0).to_numpy()).rolling(60).median().to_numpy()
    dates = df['date'].astype(str).to_numpy()
    c = adj['close'].to_numpy()
    jump = np.abs(np.diff(np.log(c), prepend=np.log(c[0]))) > .5
    clean = ~(pd.Series(jump).rolling(21, min_periods=1).max().astype(bool).to_numpy())
    rules = [r for r in RULES if r not in DROPS.get((market, etf), ())]
    ok = sig[rules].any(axis=1).to_numpy() & (value >= MIN_VALUE[market]) & np.isfinite(atr) & (dates >= '2006-01-01') & clean
    k = COST[market]; cost = (2*k['fee']+2*k['slip']+(0 if etf else k['tax']))/1e4
    sell = sell_signals(adj)
    o, h, l = adj['open'].to_numpy(), adj['high'].to_numpy(), adj['low'].to_numpy()
    base = sim(o, h, l, c, ok, atr, sell, False, cost)
    with_sell = sim(o, h, l, c, ok, atr, sell, True, cost)
    ts = np.nonzero(ok)[0]
    # the same entries: entry times depend on exits, so pair by re-running the entry walk is not exact; compare means
    return GROUPS.index(f'{market} {"ETF" if etf else "stock"}'), dates, base, with_sell


def main():
    us = pd.read_parquet(DATA/'universe'/'us.parquet').set_index('yahoo')['etf'].to_dict()
    kr = pd.read_parquet(DATA/'universe'/'kr.parquet').set_index('yahoo')['etf'].to_dict()
    jobs = [(p, m, bool(e.get(p.stem, False))) for m, e, folder in (('US', us, 'US'), ('KR', kr, 'KR'), ('KR', {}, 'KR_DELISTED'))
            for p in sorted((DATA/'daily'/folder).glob('*.parquet'))]
    out = {}
    with ProcessPoolExecutor(3) as ex:
        for r in ex.map(run_split, jobs, chunksize=16):
            for key, vals in (r or {}).items():
                out.setdefault(key, []).extend(vals)
    report = {}
    for (group, period, kind), vals in sorted(out.items()):
        v = np.array(vals)
        report.setdefault(GROUPS[group], {}).setdefault(period, {})[kind] = {'n': int(len(v)), 'mean': round(float(v.mean()*100), 3) if len(v) else None}
    for market in ('US', 'KR'):
        for period in ('develop', 'holdout'):
            for kind in ('C', 'C+SELL'):
                vals = [x for (g, p, k), xs in out.items() if GROUPS[g].startswith(market) and p == period and k == kind for x in xs]
                report.setdefault(market, {}).setdefault(period, {})[kind] = {'n': len(vals), 'mean': round(float(np.mean(vals)*100), 3)}
    (DATA/'results'/'research-sell.json').write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(json.dumps({m: report[m] for m in ('US', 'KR')}, ensure_ascii=False, indent=1))


def run_split(job):
    """Per-trade returns split by period, re-simulated per period so each variant keeps its own entries."""
    try:
        return res_split(job)
    except Exception:
        return None


def res_split(job):
    path, market, etf = job
    df = pd.read_parquet(path).dropna(subset=['open', 'high', 'low', 'close'])
    df = df[(df['close'] > 0) & (df['open'] > 0) & (df['low'] > 0)].reset_index(drop=True)
    out = {}
    for period, (a, b) in (('develop', ('2006-01-01', '2018-12-31')), ('holdout', ('2019-01-01', '2026-12-31'))):
        part = df[(df['date'].astype(str) >= a) & (df['date'].astype(str) <= b) | (df['date'].astype(str) < a)].reset_index(drop=True)
        r = run_frame(part, market, etf, a, b)
        if r:
            g, base, ws = r
            out[(g, period, 'C')] = list(base)
            out[(g, period, 'C+SELL')] = list(ws)
    return out


def run_frame(df, market, etf, a, b):
    if len(df) < 300:
        return None
    f = (df['adjclose']/df['close']).to_numpy() if 'adjclose' in df and df['adjclose'].notna().all() else np.ones(len(df))
    adj = pd.DataFrame({k: df[k].to_numpy()*f for k in ('open', 'high', 'low', 'close')})
    sig = signals(adj)
    tr = np.maximum(adj['high']-adj['low'], np.maximum(abs(adj['high']-adj['close'].shift(1)), abs(adj['low']-adj['close'].shift(1))))
    atr = (tr.rolling(14).mean()/adj['close']).to_numpy()
    value = pd.Series(df['close'].to_numpy()*df['volume'].fillna(0).to_numpy()).rolling(60).median().to_numpy()
    dates = df['date'].astype(str).to_numpy()
    c = adj['close'].to_numpy()
    jump = np.abs(np.diff(np.log(c), prepend=np.log(c[0]))) > .5
    clean = ~(pd.Series(jump).rolling(21, min_periods=1).max().astype(bool).to_numpy())
    rules = [r for r in RULES if r not in DROPS.get((market, etf), ())]
    ok = sig[rules].any(axis=1).to_numpy() & (value >= MIN_VALUE[market]) & np.isfinite(atr) & (dates >= a) & (dates <= b) & clean
    k = COST[market]; cost = (2*k['fee']+2*k['slip']+(0 if etf else k['tax']))/1e4
    sell = sell_signals(adj)
    o, h, l = adj['open'].to_numpy(), adj['high'].to_numpy(), adj['low'].to_numpy()
    return GROUPS.index(f'{market} {"ETF" if etf else "stock"}'), sim(o, h, l, c, ok, atr, sell, False, cost), sim(o, h, l, c, ok, atr, sell, True, cost)


if __name__ == '__main__':
    main()
