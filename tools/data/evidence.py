"""Evidence packs for the site's AI desk, rebuilt every day from the history archive (no AI, no Toss).

For every name in the site's catalogue it writes DATA/evidence/{site symbol}.json: what the archive says about buying it
now, as facts the analysts must cite. Everything uses only information known at the last completed daily bar.

- signals_now: the site's four rules on the last completed bar (vectorised backtest.signals, identical to app/rules.py).
- group_base_rates: each rule's 2006-2026 result for this market and instrument type, from the pre-registered research
  (results/research.json, exit C, real costs): develop 2006-2018 and holdout 2019-2026, and whether the site's research
  filter drops it.
- own_history: the same rules on THIS name since 2006, replayed with the site's exit (C) and real costs, plus plain
  forward returns 5 and 21 sessions after each signal.
- analogs: on this name, past days with the same set of firing BUY rules in the same market regime (index above or below
  its 200-day average): forward 21-session returns.
- regime: index vs its 50/200-day averages, VIX level and its 1-year percentile, US 10-year yield, USD/KRW, dollar index.
- fundamentals: US from SEC XBRL (last quarters' revenue, operating income, net income, diluted EPS with filing dates, YoY,
  next report estimated from the last 10-Q/10-K + ~91 days); KR from DART (latest statements: revenue, operating income,
  net income with the year-ago quarter, filing date) and the last 30 days of disclosure titles. ETFs: none.

Usage: evidence.py   (needs /app mounted read-only for the site's catalogue)
"""
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from backtest import COST, signals
from research import BASE, regime_masks, simulate

DATA = Path(os.environ.get('DATA', '/data'))
RULES = ('golden_cross', 'momentum', 'mean_reversion', 'breakout')
DROPS = {('KR', False): ('golden_cross', 'momentum', 'breakout'), ('US', False): ('momentum', 'breakout'), ('US', True): ('breakout',)}
INDEX = {'US': '^GSPC', 'KR': '^KS11'}


def r(x, n=2):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), n)


def dist(values):
    v = np.asarray([x for x in values if np.isfinite(x)], float)
    if not len(v):
        return {'n': 0}
    return {'n': int(len(v)), 'mean_pct': r(v.mean()*100), 'median_pct': r(np.median(v)*100), 'up_share_pct': r((v > 0).mean()*100, 1)}


def price_file(symbol, market):
    if market == 'US':
        return DATA/'daily'/'US'/f'{symbol}.parquet'
    for suffix in ('.KS', '.KQ'):
        p = DATA/'daily'/'KR'/f'{symbol}{suffix}.parquet'
        if p.exists():
            return p
    return DATA/'daily'/'KR'/f'{symbol}.KS.parquet'


def macro(name):
    df = pd.read_parquet(DATA/'daily'/'MACRO'/f'{name}.parquet')[['date', 'close']].dropna()
    return df.set_index('date')['close'].sort_index()


def regime(market):
    idx, vix = macro(INDEX[market]), macro('^VIX')
    last = idx.index[-1]
    out = {'index': INDEX[market], 'as_of': last, 'index_vs_sma50_pct': r((idx.iloc[-1]/idx.rolling(50).mean().iloc[-1]-1)*100),
           'index_vs_sma200_pct': r((idx.iloc[-1]/idx.rolling(200).mean().iloc[-1]-1)*100),
           'index_1m_pct': r((idx.iloc[-1]/idx.iloc[-22]-1)*100), 'vix': r(vix.iloc[-1]),
           'vix_1y_percentile': r((vix.iloc[-252:] < vix.iloc[-1]).mean()*100, 0)}
    for key, name, scale in (('us10y_pct', '^TNX', 1), ('usdkrw', 'KRW=X', 1), ('dollar_index', 'DX-Y.NYB', 1)):
        try:
            out[key] = r(macro(name).iloc[-1]/scale)
        except Exception:
            out[key] = None
    return out


def group_rates(research, market, etf):
    group = f'{market} {"ETF" if etf else "stock"}'
    out = {}
    for v in research['variants'].values():
        p = v['params']
        if 'Q1' not in v['questions'] or p['sig'] not in RULES:
            continue
        d, h = v['develop'][group], v['holdout'][group]
        out[p['sig']] = {'develop_2006_2018': {'trades': d.get('n'), 'mean_pct': d.get('mean'), 'ci95_pct': d.get('ci'), 'win_pct': d.get('win')},
                         'holdout_2019_2026': {'trades': h.get('n'), 'mean_pct': h.get('mean'), 'win_pct': h.get('win')},
                         'dropped_by_site_filter': p['sig'] in DROPS.get((market, etf), ())}
    return out


def long_term(df):
    """The name's whole history in a few numbers, on dividend-adjusted closes (total return), plus thinned series for the
    dashboard's long charts (weekly for five years, monthly for everything). Only completed bars are used."""
    df = df.dropna(subset=['close'])
    df = df[df['close'] > 0].reset_index(drop=True)
    f = (df['adjclose']/df['close']).to_numpy() if 'adjclose' in df and df['adjclose'].notna().all() else np.ones(len(df))
    c = pd.Series(df['close'].to_numpy()*f, index=pd.to_datetime(df['date']))
    last, n = c.iloc[-1], len(c)
    ret = lambda days: r((last/c.iloc[-days-1]-1)*100) if n > days else None
    year = c.iloc[-252:]
    peak = c.cummax()
    dd = c/peak-1
    trough = dd.idxmin()
    recovered = c[trough:][c[trough:] >= peak[trough]]
    yearly = c.groupby(c.index.year).last().pct_change().dropna().tail(10)
    summary = {'history_years': r(n/252, 1), 'first_bar': str(c.index[0].date()),
               'ret_1y_pct': ret(252), 'ret_3y_pct': ret(756), 'ret_5y_pct': ret(1260), 'ret_10y_pct': ret(2520),
               'high_52w': r(year.max(), 4), 'low_52w': r(year.min(), 4),
               'position_in_52w_pct': r((last-year.min())/(year.max()-year.min())*100, 0) if year.max() > year.min() else None,
               'from_all_time_high_pct': r((last/c.max()-1)*100),
               'max_drawdown_pct': r(dd.min()*100), 'max_drawdown_bottom': str(trough.date()),
               'max_drawdown_recovery_days': int((recovered.index[0]-trough).days) if len(recovered) else None,
               'yearly_return_pct': {str(y): r(v*100) for y, v in yearly.items()},
               'note': '배당 반영 종가 기준. 수익률·낙폭은 배당 재투자를 가정한 값'}
    weekly = c.iloc[-1260:].resample('W-FRI').last().dropna()
    monthly = c.resample('ME').last().dropna()
    series = {'weekly': [[str(d.date()), r(v, 4)] for d, v in weekly.items()],
              'monthly': [[str(d.date()), r(v, 4)] for d, v in monthly.items()]}
    return summary, series


def own(df, market, etf):
    """Own-history replay with the site's exit and costs, forward returns and analogs."""
    df = df.dropna(subset=['open', 'high', 'low', 'close'])
    df = df[(df['close'] > 0) & (df['open'] > 0) & (df['low'] > 0)].reset_index(drop=True)
    f = (df['adjclose']/df['close']).to_numpy() if 'adjclose' in df and df['adjclose'].notna().all() else np.ones(len(df))
    adj = pd.DataFrame({k: df[k].to_numpy()*f for k in ('open', 'high', 'low', 'close')})
    sig = signals(adj)
    tr = np.maximum(adj['high']-adj['low'], np.maximum(abs(adj['high']-adj['close'].shift(1)), abs(adj['low']-adj['close'].shift(1))))
    atr = (tr.rolling(14).mean()/adj['close']).to_numpy()
    dates = df['date'].astype(str).to_numpy()
    c = adj['close'].to_numpy()
    k = COST[market]
    cost = (2*k['fee']+2*k['slip']+(0 if etf else k['tax']))/1e4
    since = dates >= '2006-01-01'
    fwd5 = pd.Series(c).shift(-5).to_numpy()/c-1
    fwd21 = pd.Series(c).shift(-21).to_numpy()/c-1
    out = {'first_bar': dates[0], 'last_bar': dates[-1], 'rules': {}}
    for rule in RULES:
        mask = sig[rule].to_numpy() & since & np.isfinite(atr)
        ts, rets, helds = simulate(adj['open'].to_numpy(), adj['high'].to_numpy(), adj['low'].to_numpy(), c, mask, atr,
                                   BASE['stop'], BASE['take'], BASE['hold'], BASE['arm'], BASE['trail'], cost)
        out['rules'][rule] = {'replay_site_exit': {**dist(rets), 'avg_hold_days': r(helds.mean(), 1) if len(helds) else None},
                              'forward_5d': dist(fwd5[mask]), 'forward_21d': dist(fwd21[mask])}
    now = {rule: bool(sig[rule].iloc[-1]) for rule in RULES}
    out['signals_now'] = {k: 'BUY' if v else None for k, v in now.items()}
    firing = [k for k, v in now.items() if v]
    up = regime_masks(dates, market)['sma200']
    if firing:
        quiet = [k for k in RULES if k not in firing]
        # Exactly the same set of rules: the ones firing now fired then, and the ones quiet now were quiet then.
        same = (np.all([sig[k].to_numpy() for k in firing], axis=0)
                & (~np.any([sig[k].to_numpy() for k in quiet], axis=0) if quiet else True) & since & (up == up[-1]))
        same[-1] = False
        out['analogs'] = {'rules': firing, 'index_above_sma200': bool(up[-1]), 'forward_21d': dist(fwd21[same]),
                          'forward_5d': dist(fwd5[same])}
    # Where the name stands now (completed bar): drawdown from the 1-year high, 1-month and 3-month returns, ATR.
    out['now'] = {'close': r(df['close'].iloc[-1], 4), 'ret_1m_pct': r((c[-1]/c[-22]-1)*100), 'ret_3m_pct': r((c[-1]/c[-64]-1)*100),
                  'from_1y_high_pct': r((c[-1]/c[-252:].max()-1)*100), 'atr14_pct': r(atr[-1]*100)}
    return out


# ---- fundamentals -------------------------------------------------------------------------------------------------------------

US_TAGS = {'revenue': ('RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues', 'SalesRevenueNet'),
           'operating_income': ('OperatingIncomeLoss',), 'net_income': ('NetIncomeLoss',), 'eps_diluted': ('EarningsPerShareDiluted',)}


def us_fundamentals(con, cik):
    tags = sorted({t for ts in US_TAGS.values() for t in ts})
    q = con.execute("select tag, start, \"end\", val, form, filed from read_parquet(?) where cik = ? and tag in ("
                    + ','.join('?'*len(tags)) + ") and form in ('10-Q','10-K','10-Q/A','10-K/A')",
                    [str(DATA/'sec'/'facts'/'*.parquet'), cik, *tags]).df()
    if q.empty:
        return None
    q = q.dropna(subset=['start', 'end'])
    q['days'] = (pd.to_datetime(q['end'])-pd.to_datetime(q['start'])).dt.days
    q = q[(q['days'] >= 80) & (q['days'] <= 100)].sort_values('filed').drop_duplicates(['tag', 'end'], keep='last')
    out = {}
    for key, choices in US_TAGS.items():
        rows = next((q[q['tag'] == t] for t in choices if (q['tag'] == t).any()), None)
        if rows is None or rows.empty:
            continue
        rows = rows.sort_values('end')
        last = rows.iloc[-1]
        end = pd.to_datetime(last['end'])
        prior = rows[(pd.to_datetime(rows['end']) - (end-timedelta(days=365))).abs() <= timedelta(days=20)]
        yoy = (last['val']/prior.iloc[-1]['val']-1)*100 if len(prior) and prior.iloc[-1]['val'] not in (0, None) and prior.iloc[-1]['val'] > 0 else None
        out[key] = {'quarters': [{'end': x['end'], 'value': r(x['val'], 4), 'filed': x['filed']} for _, x in rows.tail(4).iterrows()],
                    'latest_yoy_pct': r(yoy)}
    filings = con.execute("select form, filed from read_parquet(?) where cik = ? and form in ('10-Q','10-K') order by filed desc limit 1",
                          [str(DATA/'sec'/'filings'/'*.parquet'), cik]).fetchall()
    if filings:
        last = date.fromisoformat(str(filings[0][1])[:10])
        out['last_report'] = {'form': filings[0][0], 'filed': last.isoformat(),
                              'next_report_estimate': (last+timedelta(days=91)).isoformat(),
                              'note': '다음 실적 공시 예상일은 직전 10-Q/10-K 제출일 + 91일로 추정한 값이며 확정 일정이 아닙니다.'}
    return {'source': 'SEC EDGAR XBRL (filed = 공개된 날)', **out}


KR_ACCOUNTS = {'revenue': ('ifrs-full_Revenue',), 'operating_income': ('dart_OperatingIncomeLoss',), 'net_income': ('ifrs-full_ProfitLoss',)}
REPORT_NAMES = {'11011': '사업보고서', '11012': '반기보고서', '11013': '1분기보고서', '11014': '3분기보고서'}


def num(x):
    try:
        return float(str(x).replace(',', ''))
    except (TypeError, ValueError):
        return None


def kr_fundamentals(con, code):
    # Newest reporting period first: within a year annual (Dec) > Q3 (Sep) > half (Jun) > Q1 (Mar). A plain string sort put
    # the Q1 report (11013) ahead of the half-year report (11012).
    period = {'11013': 1, '11012': 2, '11014': 3, '11011': 4}
    folders = [p.name for p in (DATA/'dart'/'fin').glob('*_*') if p.is_dir()] if (DATA/'dart'/'fin').exists() else []
    slots = sorted((s for s in folders if s.split('_')[1] in period), key=lambda s: (int(s.split('_')[0]), period[s.split('_')[1]]), reverse=True)
    out = {'source': 'DART 오픈API (접수번호 앞 8자리 = 공시일)'}
    for slot in slots:
        rows = con.execute("select * from read_parquet(?) where stock_code = ? and sj_div in ('IS','CIS')",
                           [str(DATA/'dart'/'fin'/slot/'*.parquet'), code]).df()
        if rows.empty:
            continue
        year, report = slot.split('_')
        out['report'] = {'year': year, 'kind': REPORT_NAMES.get(report, report), 'filed': str(rows['rcept_no'].iloc[0])[:8],
                         'statement': '연결' if rows['fs_div'].iloc[0] == 'CFS' else '별도'}
        for key, ids in KR_ACCOUNTS.items():
            hit = rows[rows['account_id'].isin(ids)]
            if hit.empty:
                continue
            x = hit.iloc[0]
            this, prior = num(x.get('thstrm_amount')), num(x.get('frmtrm_q_amount')) or num(x.get('frmtrm_amount'))
            out[key] = {'this_period': this, 'year_ago_period': prior,
                        'yoy_pct': r((this/prior-1)*100) if this is not None and prior and prior > 0 else None,
                        'cumulative': num(x.get('thstrm_add_amount'))}
        break
    since = (date.today()-timedelta(days=30)).strftime('%Y%m%d')
    if (DATA/'dart'/'list').exists():
        recent = con.execute("select rcept_dt, report_nm from read_parquet(?) where stock_code = ? and rcept_dt >= ? order by rcept_dt desc limit 15",
                             [str(DATA/'dart'/'list'/'*.parquet'), code, since]).fetchall()
        out['disclosures_30d'] = [{'date': d, 'title': str(t).strip()} for d, t in recent]
    return out if len(out) > 1 else None


def dividends(path, market):
    """Ex-dividend dates (exchange local date) and cash per share of the last 400 days, from the archive's Yahoo events."""
    from zoneinfo import ZoneInfo
    epath = path.parent/'events'/f'{path.stem}.json'
    try:
        items = (json.loads(epath.read_text()).get('dividends') or {}).values()
    except (OSError, ValueError):
        return []
    zone = ZoneInfo('Asia/Seoul' if market == 'KR' else 'America/New_York')
    since = time.time()-400*86400
    out = [{'ex_date': datetime.fromtimestamp(int(x['date']), zone).date().isoformat(), 'amount': float(x['amount'])}
           for x in items if isinstance(x, dict) and x.get('date') and int(x['date']) >= since and float(x.get('amount') or 0) > 0]
    return sorted(out, key=lambda x: x['ex_date'])


def main():
    sys.path.insert(0, '/app')
    from app.instruments import SYMBOLS
    research = json.loads((DATA/'results'/'research.json').read_text())
    us_etf = pd.read_parquet(DATA/'universe'/'us.parquet').set_index('symbol')['etf'].to_dict()
    kr_etf = pd.read_parquet(DATA/'universe'/'kr.parquet').set_index('symbol')['etf'].to_dict()
    tickers = pd.read_parquet(DATA/'sec'/'tickers.parquet').set_index('ticker')['cik'].to_dict() if (DATA/'sec'/'tickers.parquet').exists() else {}
    con = duckdb.connect()
    regimes = {m: regime(m) for m in ('US', 'KR')}
    out_dir = DATA/'evidence'
    out_dir.mkdir(exist_ok=True)
    built = 0
    for symbol, item in SYMBOLS.items():
        market = item['market']
        etf = bool(item.get('etf') or item.get('leveraged_etf') or (us_etf if market == 'US' else kr_etf).get(symbol))
        path = price_file(symbol, market)
        if not path.exists():
            continue
        try:
            history = own(pd.read_parquet(path), market, etf)
        except Exception as exc:
            print('skip', symbol, type(exc).__name__, exc, flush=True)
            continue
        try:
            long_summary, long_series = long_term(pd.read_parquet(path))
        except Exception:
            long_summary, long_series = None, None
        pack = {'symbol': symbol, 'name': item['name'], 'market': market, 'etf': etf, 'built_at': datetime.now().isoformat(timespec='seconds'),
                'long_term': long_summary, 'series': long_series,
                'as_of_bar': history['last_bar'],
                'method': '과거 20년 데이터(2006~, 국내는 상장 폐지 종목 포함)로 서버가 계산한 사실. 규칙대로 샀을 때의 결과이며 AI 판단의 결과가 아님. '
                          '비용(수수료·세금·슬리피지)과 사이트의 청산 방식(추적 손절, 21거래일) 반영.',
                'signals_now': history['signals_now'], 'now': history['now'],
                'group_base_rates': group_rates(research, market, etf), 'own_history': history['rules'],
                'analogs': history.get('analogs'), 'regime': regimes[market], 'dividends': dividends(path, market)}
        try:
            if not etf and market == 'US' and symbol in tickers:
                pack['fundamentals'] = us_fundamentals(con, int(tickers[symbol]))
            elif not etf and market == 'KR':
                pack['fundamentals'] = kr_fundamentals(con, symbol)
        except Exception as exc:
            pack['fundamentals_error'] = f'{type(exc).__name__}: {exc}'[:200]
        # Prices that stopped updating make a pack the app must not take as current (the app checks the date too).
        last_bar = datetime.fromisoformat(pack['as_of_bar'])
        if (datetime.now()-last_bar).days > 8:
            pack['stale'] = True
        tmp = out_dir/f'{symbol}.json.tmp'
        tmp.write_text(json.dumps(pack, ensure_ascii=False, default=str))
        tmp.replace(out_dir/f'{symbol}.json')
        built += 1
    print(f'evidence: {built} packs', flush=True)


if __name__ == '__main__':
    main()
