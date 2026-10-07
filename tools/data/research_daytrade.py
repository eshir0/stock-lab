"""Part A of research_plan_market.json: does any simple Korean day-trading style earn more than its costs?

Hourly bars (Yahoo, ~600 most traded Korean names, 2023-10 .. 2026-10). Every trade opens and closes inside one session.
Variants, costs, split and the adoption rule are fixed in research_plan_market.json; this script only executes them.
Writes results/research-daytrade.json.
"""
import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(os.environ.get('DATA', '/data'))
SPLIT = '2025-04-01'
COST = {'stock': .0033, 'etf': .0013}
VARIANTS = ('D0', 'D1', 'D2', 'D3', 'D4', 'D5')


def daily_signals(symbol):
    """The site's four daily BUY rules as of each close (backtest.signals), keyed by date."""
    from backtest import signals
    path = DATA/'daily'/'KR'/f'{symbol}.parquet'
    if not path.exists():
        return {}
    df = pd.read_parquet(path).dropna(subset=['open', 'high', 'low', 'close'])
    df = df[df['close'] > 0].reset_index(drop=True)
    if len(df) < 80:
        return {}
    sig = signals(df[['open', 'high', 'low', 'close']]).any(axis=1).to_numpy()
    return dict(zip(df['date'].astype(str), sig))


def sessions(path):
    df = pd.read_parquet(path).dropna(subset=['open', 'high', 'low', 'close'])
    df = df[(df['close'] > 0) & (df['open'] > 0)]
    local = df['time'].dt.tz_convert('Asia/Seoul')
    df = df.assign(date=local.dt.date.astype(str), hour=local.dt.hour)
    df = df[(df['hour'] >= 9) & (df['hour'] <= 15)].sort_values('time')
    out = []
    for date, g in df.groupby('date', sort=True):
        if g['hour'].iloc[0] != 9 or len(g) < 4:
            continue
        out.append({'date': date, 'open': g['open'].to_numpy(), 'high': g['high'].to_numpy(), 'low': g['low'].to_numpy(),
                    'close': g['close'].to_numpy(), 'volume': g['volume'].to_numpy()})
    return out


def run_symbol(path, etf):
    symbol = path.stem
    days = sessions(path)
    sig = daily_signals(symbol)
    cost = COST['etf' if etf else 'stock']
    rows = []
    first_vol = []
    for k, d in enumerate(days):
        if k == 0:
            first_vol.append(d['volume'][0]); continue
        prev = days[k-1]
        prev_close, o, close = prev['close'][-1], d['open'][0], d['close'][-1]
        gap = o/prev_close-1
        if abs(gap) > .3:                                  # bad print or a corporate action
            first_vol.append(d['volume'][0]); continue
        avg_vol = np.mean(first_vol[-20:]) if len(first_vol) >= 20 else None
        h1_close, h1_high, h1_low, h1_vol = d['close'][0], d['high'][0], d['low'][0], d['volume'][0]
        later_open = d['open'][1]
        base = {'date': d['date'], 'symbol': symbol, 'etf': etf}

        def add(variant, entry, exit_price):
            rows.append({**base, 'v': variant, 'ret': exit_price/entry-1-cost})
        add('D0', o, close)
        if gap >= .02:
            add('D1', later_open, close)
        if h1_close/o-1 >= .01 and avg_vol and avg_vol > 0 and h1_vol >= 1.5*avg_vol:
            add('D2', later_open, close)
        if gap <= -.02:
            add('D3', o, close)
        if sig.get(prev['date']):
            add('D4', o, close)
        for i in range(1, len(d['close'])):                 # opening range breakout after the first hour
            if d['low'][i] <= h1_low and d['high'][i] < h1_high:
                break                                       # the range broke down first: no trade that day
            if d['high'][i] > h1_high:
                entry = max(h1_high, d['open'][i])          # a gap over the high fills at the open, never better
                stop_hit = any(d['low'][j] <= h1_low for j in range(i+1, len(d['close'])))
                exit_price = h1_low if stop_hit else close
                add('D5', entry, exit_price)
                break
        first_vol.append(h1_vol)
    return rows


def stats(df):
    """Mean per trade (%), a 95% interval that treats each month as one draw, the win rate and the count."""
    if len(df) == 0:
        return {'n': 0}
    by_month = df.groupby(df['date'].str[:7])['ret'].mean()
    mean = df['ret'].mean()
    n_m = len(by_month)
    se = by_month.std(ddof=1)/math.sqrt(n_m) if n_m >= 3 else None
    r = lambda x: round(float(x)*100, 3)
    return {'n': int(len(df)), 'months': n_m, 'mean_pct': r(mean), 'ci95_pct': [r(mean-1.96*se), r(mean+1.96*se)] if se else None,
            'win_pct': round(float((df['ret'] > 0).mean())*100, 1)}


def main():
    etf = pd.read_parquet(DATA/'universe'/'kr.parquet').set_index('yahoo')['etf'].to_dict()
    rows = []
    paths = sorted((DATA/'hourly'/'KR').glob('*.parquet'))
    for n, path in enumerate(paths, 1):
        try:
            rows += run_symbol(path, bool(etf.get(path.stem)))
        except Exception as exc:
            print('skip', path.stem, type(exc).__name__, exc, flush=True)
        if n % 100 == 0:
            print(f'{n}/{len(paths)} names', flush=True)
    df = pd.DataFrame(rows)
    out = {'plan': json.loads((Path(__file__).with_name('research_plan_market.json')).read_text())['part_A_korea_day_trading'],
           'names': len(paths), 'results': {}, 'adopted': []}
    for v in VARIANTS:
        part = df[df['v'] == v]
        res = {}
        for group, g in (('KR stock', part[~part['etf']]), ('KR ETF', part[part['etf']])):
            res[group] = {'first_half': stats(g[g['date'] < SPLIT]), 'second_half': stats(g[g['date'] >= SPLIT])}
        out['results'][v] = res
        s = res['KR stock']
        ok = all(h.get('n', 0) >= 300 and h.get('mean_pct', -1) > 0 and h.get('ci95_pct') and h['ci95_pct'][0] > 0
                 for h in (s['first_half'], s['second_half']))
        if ok:
            out['adopted'].append(v)
    (DATA/'results').mkdir(exist_ok=True)
    (DATA/'results'/'research-daytrade.json').write_text(json.dumps(out, ensure_ascii=False, indent=1))
    for v in VARIANTS:
        for group in ('KR stock', 'KR ETF'):
            r = out['results'][v][group]
            print(v, group, 'H1', r['first_half'], '| H2', r['second_half'], flush=True)
    print('adopted:', out['adopted'] or 'none')


if __name__ == '__main__':
    main()
