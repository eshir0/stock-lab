"""Q7: a daily account simulation of the rule baseline on the site's own pool (research_plan.json).

One account per currency, cash only, the site's sizing: risk 0.5% of equity per trade at the stop distance, at most 30% of
equity per name and per order, KR whole shares, US fractional (4 decimals). Entries at the next open after a rule BUY, in
order of traded value; exits as in research.simulate (C: trailing stop or holding limit; conservative bar reading).
Real costs. Leveraged ETFs are left out (the next experiment runs without them). Equity is marked at every close, so the
drawdown is the daily one the verification plan uses. Compared with holding the index ETF (KODEX 200 / SPY).

The pool is today's list of large names, so the absolute return carries hindsight; the drawdown and the comparison of
settings are the useful part.

Usage: portfolio.py '{"stop": 1.5, ...}'   (any research parameter; defaults = research.BASE)
"""
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import COST, signals
from research import BASE, SIGNALS, regime_masks

DATA = Path(os.environ.get('DATA', '/data'))
SEED = {'KR': float(os.environ.get('SEED_KR', 10_000_000)), 'US': float(os.environ.get('SEED_US', 10_000))}
BENCH = {'KR': '069500.KS', 'US': 'SPY'}
RISK, NAME_CAP = .005, .30
# The site's research filter (app/gate.py RESEARCH_DROPS), keyed by (market, is ETF).
DROPS = {('KR', False): ('golden_cross', 'momentum', 'breakout'), ('US', False): ('momentum', 'breakout'), ('US', True): ('breakout',)}


def load(path, market, params):
    df = pd.read_parquet(path).dropna(subset=['open', 'high', 'low', 'close'])
    df = df[(df['close'] > 0) & (df['open'] > 0) & (df['low'] > 0)].reset_index(drop=True)
    f = (df['adjclose']/df['close']).to_numpy() if df['adjclose'].notna().all() else np.ones(len(df))
    adj = pd.DataFrame({k: df[k]*f for k in ('open', 'high', 'low', 'close')})
    sig = signals(adj)
    tr = np.maximum(adj['high']-adj['low'], np.maximum(abs(adj['high']-adj['close'].shift(1)), abs(adj['low']-adj['close'].shift(1))))
    atr = tr.rolling(14).mean()/adj['close']
    dates = df['date'].astype(str).to_numpy()
    rules = [r for r in SIGNALS[params['sig']] if r not in DROPS.get((market, path.stem in ETFS), ()) or params.get('filter') != 'research']
    buy = (sig[rules].any(axis=1).to_numpy() if rules else np.zeros(len(sig), bool)) & regime_masks(dates, market)[params['regime']]
    value = pd.Series(df['close']*df['volume'].fillna(0)).rolling(60).median()
    cols = {'open': adj['open'], 'high': adj['high'], 'low': adj['low'], 'close': adj['close'], 'atr': atr, 'buy': buy, 'value': value}
    return pd.DataFrame({k: np.asarray(v) for k, v in cols.items()}, index=dates)   # positional: the frames above have a range index


def run(market, symbols, params, start, end):
    data = {}
    for s in symbols:
        folder = 'US' if market == 'US' else 'KR'
        path = DATA/'daily'/folder/f'{s}.parquet'
        if path.exists():
            data[s] = load(path, market, params)
    days = sorted(set().union(*[set(d.index) for d in data.values()]))
    days = [d for d in days if start <= d <= end]
    k = COST[market]
    etf = {s: s in ETFS for s in data}
    cash, pos, equity_curve, trades = SEED[market], {}, [], []
    whole = market == 'KR'
    pending, last, peak, skipped = {}, {}, SEED[market], 0
    limit = params.get('daily_limit')
    prev_eq = SEED[market]
    for day in days:
        halted = False
        if limit and pos:
            open_eq = cash+sum(p['qty']*(data[x].at[day, 'open'] if day in data[x].index else last.get(x, p['entry'])) for x, p in pos.items())
            if open_eq/prev_eq-1 <= -limit:
                halted = True
                for s in list(pos):
                    if day in data[s].index:
                        cash += sell_all(s, data[s].at[day, 'open'], pos, trades, day, k, etf)
                pending = {}
        # 1) yesterday's signals buy at today's open, sized on what is known then: yesterday's closes (2026-10-03 review:
        #    the closes used to be updated first, so the size used today's close before it existed)
        for s in pending:
            d = data[s]
            if day not in d.index or s in pos:
                continue
            row = d.loc[day]
            entry = row['open']*(1+k['slip']/1e4)
            sp = min(max(params['stop']*pending[s], .02), .15)
            tp = min(max(params['take']*sp, .03), .40)
            eq = cash+sum(p['qty']*last.get(x, p['entry']) for x, p in pos.items())
            risk = params.get('risk', RISK)
            if params.get('risk_mode') == 'drawdown':
                risk *= max(.5, 1-max(0.0, 1-eq/peak)/.20)
            qty = min(eq*risk/(entry*sp), eq*NAME_CAP/entry, cash/(entry*(1+k['fee']/1e4)))
            qty = math.floor(qty) if whole else math.floor(qty*1e4)/1e4
            if qty <= 0:
                skipped += 1
                continue
            gross = qty*entry
            cash -= gross*(1+k['fee']/1e4)
            pos[s] = {'qty': qty, 'entry': entry, 'stop': entry*(1-sp), 'arm': entry*(1+tp*params['arm']),
                      'trail': sp*params['trail'], 'high': entry, 'left': int(params['hold']), 'day': day, 'cost': gross}
        pending = {}
        # 2) exits through today's bar
        for s in list(pos):
            d = data[s]
            if day not in d.index:
                continue
            p, row = pos[s], d.loc[day]
            price = None
            if row['open'] <= p['stop']:
                price = row['open']
            elif row['low'] <= p['stop']:
                price = p['stop']
            else:
                p['high'] = max(p['high'], row['high'])
                if p['high'] >= p['arm']:
                    p['stop'] = max(p['stop'], p['entry']*1.003, p['high']*(1-p['trail']))
                p['left'] -= 1
                if p['left'] <= 0:
                    price = row['close']
            if price is not None:
                sell = price*(1-k['slip']/1e4)
                gross = p['qty']*sell
                fee = gross*(k['fee']+(0 if etf[s] else k['tax']))/1e4
                cash += gross-fee
                trades.append((s, p['day'], day, (gross-fee)/(p['cost']*(1+k['fee']/1e4))-1))
                del pos[s]
        for s, d in data.items():
            if day in d.index:
                last[s] = d.at[day, 'close']
        if limit and not halted and pos:
            close_eq = cash+sum(p['qty']*last.get(x, p['entry']) for x, p in pos.items())
            if close_eq/prev_eq-1 <= -limit:
                halted = True
                for s in list(pos):
                    cash += sell_all(s, last.get(s, pos[s]['entry']), pos, trades, day, k, etf)
        # 3) today's signals, best traded value first
        cands = [(data[s].loc[day, 'value'], s) for s in data if s not in pos and day in data[s].index
                 and data[s].loc[day, 'buy'] and np.isfinite(data[s].loc[day, 'atr'])]
        pending = {} if halted else {s: data[s].loc[day, 'atr'] for _, s in sorted(cands, reverse=True)}
        eq = cash+sum(p['qty']*last.get(x, p['entry']) for x, p in pos.items())
        equity_curve.append((day, eq))
        prev_eq = eq
        peak = max(peak, eq)
    curve = pd.Series([e for _, e in equity_curve], index=[d for d, _ in equity_curve])
    bench = pd.read_parquet(DATA/'daily'/('KR' if market == 'KR' else 'US')/f'{BENCH[market]}.parquet').set_index('date')['adjclose']
    bench = bench[(bench.index >= curve.index[0]) & (bench.index <= curve.index[-1])]
    years = max(len(curve)/252, 1e-9)
    dd = float((curve/curve.cummax()-1).min()*100)
    bdd = float((bench/bench.cummax()-1).min()*100)
    rets = [t[3] for t in trades]
    return {'market': market, 'start': curve.index[0], 'end': curve.index[-1], 'names': len(data), 'trades': len(trades),
            'total_pct': round((curve.iloc[-1]/SEED[market]-1)*100, 1),
            'cagr_pct': round(((curve.iloc[-1]/SEED[market])**(1/years)-1)*100, 2), 'max_dd_pct': round(dd, 1),
            'index_total_pct': round((bench.iloc[-1]/bench.iloc[0]-1)*100, 1),
            'index_cagr_pct': round(((bench.iloc[-1]/bench.iloc[0])**(1/years)-1)*100, 2), 'index_max_dd_pct': round(bdd, 1),
            'skipped_zero_size': skipped, 'mar': round(((curve.iloc[-1]/SEED[market])**(1/years)-1)*100/abs(dd), 3) if dd else None,
            'win_pct': round(float(np.mean([r > 0 for r in rets])*100), 1) if rets else None,
            'avg_trade_pct': round(float(np.mean(rets)*100), 3) if rets else None,
            'worst_year_pct': round(float(curve.groupby(curve.index.str[:4]).apply(lambda x: x.iloc[-1]/x.iloc[0]-1).min()*100), 1)}


ETFS = set()


def sell_all(s, price, pos, trades, day, k, etf):
    """Sell one whole position at `price` (the daily loss limit); returns the cash it brings."""
    p = pos.pop(s)
    sell = price*(1-k['slip']/1e4)
    gross = p['qty']*sell
    fee = gross*(k['fee']+(0 if etf[s] else k['tax']))/1e4
    trades.append((s, p['day'], day, (gross-fee)/(p['cost']*(1+k['fee']/1e4))-1))
    return gross-fee


def main():
    sys.path.insert(0, '/app')
    from app.instruments import SYMBOLS
    params = {**BASE, **(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {})}
    pools = {'KR': [], 'US': []}
    kr_etf = pd.read_parquet(DATA/'universe'/'kr.parquet').set_index('symbol')['etf'].to_dict()
    us_etf = pd.read_parquet(DATA/'universe'/'us.parquet').set_index('symbol')['etf'].to_dict()
    for s, v in SYMBOLS.items():
        if v.get('leveraged_etf'):
            continue
        if v['market'] == 'US':
            pools['US'].append(s)
            if us_etf.get(s):
                ETFS.add(s)
        else:
            y = s+'.KS' if (DATA/'daily'/'KR'/f'{s}.KS.parquet').exists() else s+'.KQ'
            pools['KR'].append(y)
            if kr_etf.get(s):
                ETFS.add(y)
    out = {'params': params}
    for label, (a, b) in {'all': ('2006-01-01', '2026-12-31'), 'develop': ('2006-01-01', '2018-12-31'),
                          'holdout': ('2019-01-01', '2026-12-31')}.items():
        out[label] = {m: run(m, pools[m], params, a, b) for m in ('KR', 'US')}
    name = os.environ.get('TAG', 'base')
    (DATA/'results'/f'portfolio-{name}.json').write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
