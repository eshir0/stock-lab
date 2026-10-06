"""Q6 (research_plan_q6.json): earnings dates and simple fundamentals as filters on the site's buys. Registered before the
Korean statements were complete; run once, automatically, when they are (--if-ready).

Everything a filter uses must have been public before the signal day: statements by their filing date (SEC `filed`, DART
receipt number date), earnings releases by their actual date (US 8-K item 2.02, KR 영업(잠정)실적), which companies announce
in advance - the one approximation, stated in the report.

Usage: research_q6.py [--if-ready] [--workers 3] [--test N]   -> DATA/results/research-q6.json
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from backtest import COST, MIN_VALUE, signals
from research import BASE, simulate, stats

DATA = Path(os.environ.get('DATA', '/data'))
DEV_END, START = '2021-01-01', '2016-01-01'
VARIANTS = ('E0', 'E5', 'E10', 'E21', 'P', 'G', 'PG')
DROPS = {'KR': ('golden_cross', 'momentum', 'breakout'), 'US': ('momentum', 'breakout')}     # stocks (app/gate.py)
RULES = ('golden_cross', 'momentum', 'mean_reversion', 'breakout')
US_REVENUE = ('RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues', 'SalesRevenueNet')


def num(x):
    try:
        return float(str(x).replace(',', ''))
    except (TypeError, ValueError):
        return None


# ---- point-in-time facts -------------------------------------------------------------------------------------------------------

def us_facts(con):
    """cik -> {'releases': sorted dates, 'quarters': [(filed, end, net_income, revenue)], 'annual': [(filed, net_income)]}."""
    tags = ('NetIncomeLoss',)+US_REVENUE
    rows = con.execute("select cik, tag, start, \"end\", val, filed from read_parquet(?) where tag in ("+','.join('?'*len(tags))
                       + ") and form in ('10-Q','10-K','10-Q/A','10-K/A') and start is not null",
                       [str(DATA/'sec'/'facts'/'*.parquet'), *tags]).df()
    rows['days'] = (pd.to_datetime(rows['end'])-pd.to_datetime(rows['start'])).dt.days
    out = {}
    for cik, g in rows.groupby('cik'):
        q = g[(g['days'] >= 80) & (g['days'] <= 100)].sort_values('filed').drop_duplicates(['tag', 'end'], keep='first')
        a = g[(g['days'] >= 350) & (g['days'] <= 380) & (g['tag'] == 'NetIncomeLoss')].sort_values('filed').drop_duplicates('end', keep='first')
        ni = q[q['tag'] == 'NetIncomeLoss'].set_index('end')
        rev = next((q[q['tag'] == t].set_index('end') for t in US_REVENUE if (q['tag'] == t).any()), pd.DataFrame())
        quarters = []
        for end in sorted(set(ni.index) | set(rev.index)):
            filed = min(x for x in (ni['filed'].get(end), rev['filed'].get(end) if len(rev) else None) if x is not None)
            quarters.append((str(filed), str(end), ni['val'].get(end), rev['val'].get(end) if len(rev) else None))
        out[int(cik)] = {'quarters': quarters, 'annual': [(str(f), v) for f, v in zip(a['filed'], a['val'])]}
    rel = con.execute("select cik, filed from read_parquet(?) where form = '8-K' and items like '%2.02%'",
                      [str(DATA/'sec'/'filings'/'*.parquet')]).df()
    for cik, g in rel.groupby('cik'):
        out.setdefault(int(cik), {'quarters': [], 'annual': []})['releases'] = sorted(set(map(str, g['filed'])))
    return out


def kr_facts(con):
    """stock code -> the same shape from DART statements and 잠정실적 disclosures."""
    fin = con.execute("select stock_code, rcept_no, reprt_code, bsns_year, account_id, thstrm_amount, frmtrm_q_amount, frmtrm_amount "
                      "from read_parquet(?, union_by_name=true) where sj_div in ('IS','CIS') and account_id in "
                      "('ifrs-full_ProfitLoss','ifrs-full_Revenue')", [str(DATA/'dart'/'fin'/'*'/'*.parquet')]).df()
    out = {}
    for code, g in fin.groupby('stock_code'):
        quarters, annual = [], []
        for (rcept, report), r in g.groupby(['rcept_no', 'reprt_code']):
            filed = f'{str(rcept)[:4]}-{str(rcept)[4:6]}-{str(rcept)[6:8]}'
            ni = r[r['account_id'] == 'ifrs-full_ProfitLoss']
            rv = r[r['account_id'] == 'ifrs-full_Revenue']
            this_ni = num(ni['thstrm_amount'].iloc[0]) if len(ni) else None
            this_rv = num(rv['thstrm_amount'].iloc[0]) if len(rv) else None
            prior_rv = (num(rv['frmtrm_q_amount'].iloc[0]) or num(rv['frmtrm_amount'].iloc[0])) if len(rv) else None
            if report == '11011':
                annual.append((filed, this_ni))
            quarters.append((filed, str(r['bsns_year'].iloc[0])+'-'+report, this_ni, this_rv, prior_rv))
        out[str(code)] = {'quarters': sorted(quarters), 'annual': sorted(annual)}
    rel = con.execute("select stock_code, rcept_dt from read_parquet(?) where report_nm like '%영업(잠정)실적%'",
                      [str(DATA/'dart'/'list'/'*.parquet')]).df()
    for code, g in rel.groupby('stock_code'):
        dates = sorted({f'{d[:4]}-{d[4:6]}-{d[6:8]}' for d in g['rcept_dt'].astype(str)})
        out.setdefault(str(code), {'quarters': [], 'annual': []})['releases'] = dates
    return out


def masks(dates, facts, market):
    """{variant: bool array over `dates`} - True where the filter allows a buy on that signal day."""
    n = len(dates)
    allow = {v: np.ones(n, bool) for v in VARIANTS}
    releases = np.array(sorted(facts.get('releases') or []))
    if len(releases):
        nxt = np.searchsorted(releases, dates, side='right')                         # first release after the signal day
        idx = np.searchsorted(dates, releases)                                       # its position among the trading days
        gap = np.where(nxt < len(releases), idx[np.minimum(nxt, len(releases)-1)]-np.arange(n), 10**9)
        for k, v in ((5, 'E5'), (10, 'E10'), (21, 'E21')):
            allow[v] = ~((gap >= 1) & (gap <= k))
    q, a = facts.get('quarters') or [], facts.get('annual') or []
    profit, growth = np.zeros(n, bool), np.zeros(n, bool)
    qi = ai = 0
    last_q = last_a = None
    seen = []
    for i, day in enumerate(dates):
        while qi < len(q) and q[qi][0] < day:
            last_q = q[qi]
            seen.append(q[qi])
            qi += 1
        while ai < len(a) and a[ai][0] < day:
            last_a = a[ai]
            ai += 1
        profit[i] = bool(last_q and last_a and (last_q[2] or 0) > 0 and (last_a[1] or 0) > 0)
        if last_q:
            if market == 'KR':
                growth[i] = bool(last_q[3] and last_q[4] and last_q[3] > last_q[4])
            else:
                end = pd.Timestamp(last_q[1])
                prior = [x for x in seen if x[3] and abs((pd.Timestamp(x[1])-(end-pd.Timedelta(days=365))).days) <= 20]
                growth[i] = bool(last_q[3] and prior and last_q[3] > prior[-1][3])
    allow['P'], allow['G'], allow['PG'] = profit, growth, profit & growth
    return allow


def run_symbol(job):
    path, market, facts = job
    try:
        df = pd.read_parquet(path).dropna(subset=['open', 'high', 'low', 'close'])
    except Exception:
        return []
    df = df[(df['close'] > 0) & (df['open'] > 0) & (df['low'] > 0)].reset_index(drop=True)
    if len(df) < 300 or not facts:
        return []
    f = (df['adjclose']/df['close']).to_numpy() if 'adjclose' in df and df['adjclose'].notna().all() else np.ones(len(df))
    adj = pd.DataFrame({k: df[k].to_numpy()*f for k in ('open', 'high', 'low', 'close')})
    sig = signals(adj)
    tr = np.maximum(adj['high']-adj['low'], np.maximum(abs(adj['high']-adj['close'].shift(1)), abs(adj['low']-adj['close'].shift(1))))
    atr = (tr.rolling(14).mean()/adj['close']).to_numpy()
    value = pd.Series(df['close'].to_numpy()*df['volume'].fillna(0).to_numpy()).rolling(60).median().to_numpy()
    dates = df['date'].astype(str).to_numpy()
    c = adj['close'].to_numpy()
    jump = np.abs(np.diff(np.log(c), prepend=np.log(c[0]))) > .5
    clean = ~(pd.Series(jump).rolling(21, min_periods=1).max().astype(bool).to_numpy())       # the past only
    rules = [r for r in RULES if r not in DROPS[market]]
    base = sig[rules].any(axis=1).to_numpy() & (value >= MIN_VALUE[market]) & np.isfinite(atr) & (dates >= START) & clean
    k = COST[market]
    cost = (2*k['fee']+2*k['slip']+k['tax'])/1e4
    out = []
    for v, allow in masks(dates, facts, market).items():
        ts, rets, _ = simulate(adj['open'].to_numpy(), adj['high'].to_numpy(), adj['low'].to_numpy(), c, base & allow, atr,
                               BASE['stop'], BASE['take'], int(BASE['hold']), BASE['arm'], BASE['trail'], cost)
        if len(ts):
            months = np.array([int(d[:4])*100+int(d[5:7]) for d in dates[ts]], np.int32)
            out.append((v, market, months, (rets*100).astype(np.float32)))
    return out


def ready():
    """True once every due DART statement slot from 2015 on covers every listed company, and Q6 has not run yet."""
    if (DATA/'results'/'research-q6.json').exists():
        return False
    try:
        p = json.loads((DATA/'dart'/'progress.json').read_text())
        corps = len(pd.read_parquet(DATA/'dart'/'corps.parquet'))
    except (OSError, ValueError):
        return False
    slots = p.get('fin') or {}
    due = [f'{y}_{r}' for y in range(2015, 2026) for r in ('11011', '11012', '11013', '11014')]
    return all(len((slots.get(s) or {}).get('corps', [])) >= corps for s in due)


def main():
    a = argparse.ArgumentParser()
    a.add_argument('--if-ready', action='store_true')
    a.add_argument('--workers', type=int, default=3)
    a.add_argument('--test', type=int, default=0, help='a quick trial on N symbols per market; writes research-q6-test.json')
    args = a.parse_args()
    if args.if_ready and not ready():
        print('q6: not ready (DART statements incomplete or already run)', flush=True)
        return
    con = duckdb.connect()
    us, kr = us_facts(con), (kr_facts(con) if (DATA/'dart'/'fin').exists() else {})
    tickers = pd.read_parquet(DATA/'sec'/'tickers.parquet').set_index('ticker')['cik'].to_dict()
    us_etf = pd.read_parquet(DATA/'universe'/'us.parquet').set_index('yahoo')['etf'].to_dict()
    kr_etf = pd.read_parquet(DATA/'universe'/'kr.parquet').set_index('yahoo')['etf'].to_dict()
    jobs = []
    for path in sorted((DATA/'daily'/'US').glob('*.parquet')):
        cik = tickers.get(path.stem.replace('-', '.')) or tickers.get(path.stem)
        if cik and not us_etf.get(path.stem):
            jobs.append((path, 'US', us.get(int(cik))))
    for folder in ('KR', 'KR_DELISTED'):
        for path in sorted((DATA/'daily'/folder).glob('*.parquet')):
            code = path.stem.split('.')[0]
            if not kr_etf.get(path.stem) and code in kr:
                jobs.append((path, 'KR', kr[code]))
    if args.test:
        jobs = [j for j in jobs if j[1] == 'US'][:args.test]+[j for j in jobs if j[1] == 'KR'][:args.test]
    parts = {}
    with ProcessPoolExecutor(args.workers) as ex:
        for result in ex.map(run_symbol, jobs, chunksize=8):
            for v, market, months, rets in result:
                parts.setdefault((v, market), []).append((months, rets))
    dev_end = int(DEV_END[:4])*100+1
    report = {'plan': json.loads((Path(__file__).parent/'research_plan_q6.json').read_text()), 'names': len(jobs),
              'run_at': time.strftime('%Y-%m-%d %H:%M'), 'variants': {}}
    for v in VARIANTS:
        report['variants'][v] = {}
        for market in ('US', 'KR'):
            rows = parts.get((v, market), [])
            m = np.concatenate([x for x, _ in rows]) if rows else np.array([], np.int32)
            r = np.concatenate([x for _, x in rows]) if rows else np.array([], np.float32)
            report['variants'][v][market] = {'develop': stats(m[m < dev_end], r[m < dev_end]),
                                             'holdout': stats(m[m >= dev_end], r[m >= dev_end])}
    out = DATA/'results'/('research-q6-test.json' if args.test else 'research-q6.json')
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f'q6: {len(jobs)} names -> {out.name}', flush=True)


if __name__ == '__main__':
    main()
