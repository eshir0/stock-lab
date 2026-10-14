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
  the fourth quarter derived from the year, next report estimated from the last 10-Q/10-K + ~91 days); KR from DART (latest
  statements: revenue, operating income, net income with the year-ago quarter, filing date) and the last 30 days of
  disclosure titles. Both: balance (liabilities, equity, debt-to-equity) and valuation (PER on trailing-four-quarter EPS,
  split-adjusted, and PER at the last fiscal year ends). ETFs: none.

Usage: evidence.py   (needs /app mounted read-only for the site's catalogue)
"""
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
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


def group_rates(research, market, etf, us_long=None):
    """The market's per-rule results (research.json Q1, old exit), and for US names the same rule under the US exit of
    exit_profile=market_long (research-us-wide-signals.json) when that study exists."""
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
    for v in ((us_long or {}).get('variants') or {}).values() if market == 'US' else ():
        sig = v['params']['sig']
        if sig in out:
            d, h = v['develop'][group], v['holdout'][group]
            out[sig]['us_long'] = {'develop_2006_2018': {'trades': d.get('n'), 'mean_pct': d.get('mean'), 'ci95_pct': d.get('ci')},
                                   'holdout_2019_2026': {'trades': h.get('n'), 'mean_pct': h.get('mean')}}
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
        if market == 'US':
            # The US exit of exit_profile=market_long (research_plan_us_hold.json): stop 3x ATR, take 3x stop, 63 sessions.
            ts, rets, helds = simulate(adj['open'].to_numpy(), adj['high'].to_numpy(), adj['low'].to_numpy(), c, mask, atr,
                                       3.0, 3.0, 63, BASE['arm'], BASE['trail'], cost)
            out['rules'][rule]['replay_us_long'] = {**dist(rets), 'avg_hold_days': r(helds.mean(), 1) if len(helds) else None}
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
        # What ANY day of this name in the same market regime went on to do: an analog result only says something about
        # the signal when it differs from this baseline (a long-rising ETF goes up after almost anything).
        usual = since & (up == up[-1])
        usual[-1] = False
        out['analogs'] = {'rules': firing, 'index_above_sma200': bool(up[-1]), 'forward_21d': dist(fwd21[same]),
                          'forward_5d': dist(fwd5[same]), 'baseline_forward_21d': dist(fwd21[usual])}
    # Where the name stands now (completed bar): drawdown from the 1-year high, 1-month and 3-month returns, ATR.
    out['now'] = {'close': r(df['close'].iloc[-1], 4), 'ret_1m_pct': r((c[-1]/c[-22]-1)*100), 'ret_3m_pct': r((c[-1]/c[-64]-1)*100),
                  'from_1y_high_pct': r((c[-1]/c[-252:].max()-1)*100), 'atr14_pct': r(atr[-1]*100)}
    return out


# ---- fundamentals -------------------------------------------------------------------------------------------------------------

US_TAGS = {'revenue': ('RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues', 'SalesRevenueNet'),
           'operating_income': ('OperatingIncomeLoss',), 'net_income': ('NetIncomeLoss',), 'eps_diluted': ('EarningsPerShareDiluted',)}
US_BALANCE = ('Liabilities', 'StockholdersEquity', 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest',
              'LiabilitiesAndStockholdersEquity')
DEBT_NOTE = ('부채비율 = 부채총계 ÷ 자본총계 × 100. 은행·보험·증권사는 예금·보험계약이 부채라 구조적으로 높습니다. '
             '자본이 0 이하(자본잠식)면 비율 대신 equity가 음수로 남습니다.')
PER_NOTE = ('PER = 마지막 완성 일봉 종가 ÷ 최근 4분기 주당순이익(분할 반영). 적자면 PER 없음. per_history는 각 회계연도 말 종가 ÷ '
            '그 해 주당순이익으로, 그 해 실적이 공개되기 전 값이라 당시 투자자가 본 PER과 다릅니다. 성장 기업은 PER이 늘 높을 수 있어 '
            '과거 자기 수준과 비교하세요. 이익이 0에 가까우면(적자에서 막 벗어난 해 등) PER이 수백 배로 커져 비교 의미가 약합니다.')


def split_events(path):
    """Every split in the archive's Yahoo events for this price file: [(ex date 'YYYY-MM-DD' UTC, ratio new/old)]."""
    epath = path.parent/'events'/f'{path.stem}.json'
    try:
        items = (json.loads(epath.read_text()).get('splits') or {}).values()
    except (OSError, ValueError):
        return []
    out = []
    for x in items:
        try:
            ratio = float(x['numerator'])/float(x['denominator'])
            when = datetime.fromtimestamp(int(x['date']), timezone.utc).date().isoformat()
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        if ratio > 0 and abs(ratio-1) > .01:
            out.append((when, ratio))
    return sorted(out)


def split_factor(events, filed):
    """Shares per share-at-filing: a per-share number published on `filed` is divided by this to match today's (split-
    adjusted) prices. A number restated after the split was filed after it and needs nothing."""
    f = 1.0
    for when, ratio in events:
        if when > str(filed)[:10]:
            f *= ratio
    return f


def close_on(closes, day):
    """Last completed close on or before `day` (closes: Series indexed by 'YYYY-MM-DD', split-adjusted)."""
    hit = closes[closes.index <= str(day)[:10]]
    return float(hit.iloc[-1]) if len(hit) else None


def quarters(rows):
    """Quarterly values from SEC duration facts (start, end, val, filed; one row per period, latest filing kept). 10-Ks give
    the fourth quarter only as the year, so it is derived: the year minus the 9-month year-to-date (or minus the year's three
    quarters). -> [{'end', 'value', 'filed', 'derived'}] oldest first."""
    rows = rows.copy()
    rows['days'] = (pd.to_datetime(rows['end'])-pd.to_datetime(rows['start'])).dt.days
    out = {str(x['end']): {'end': str(x['end']), 'value': float(x['val']), 'filed': str(x['filed']), 'derived': False}
           for _, x in rows[(rows['days'] >= 80) & (rows['days'] <= 100)].iterrows()}
    for _, a in rows[(rows['days'] >= 350) & (rows['days'] <= 380)].iterrows():
        end = str(a['end'])
        if end in out:
            continue
        ytd = rows[(rows['start'] == a['start']) & (rows['days'] >= 260) & (rows['days'] <= 290)]
        if len(ytd):
            rest = float(ytd.iloc[-1]['val'])
        else:
            inside = [q for q in out.values() if str(a['start']) < q['end'] < end]
            if len(inside) != 3:
                continue
            rest = sum(q['value'] for q in inside)
        out[end] = {'end': end, 'value': float(a['val'])-rest, 'filed': str(a['filed']), 'derived': True}
    return [out[k] for k in sorted(out)]


def ttm(qs):
    """Sum of the last four quarters when they are consecutive (about a year from the first to the last end), else None."""
    if len(qs) < 4:
        return None
    last = qs[-4:]
    span = (pd.Timestamp(last[-1]['end'])-pd.Timestamp(last[0]['end'])).days
    return sum(q['value'] for q in last) if 250 <= span <= 300 else None


def debt(liabilities, equity, as_of, filed):
    if liabilities is None or equity is None:
        return None
    return {'as_of': as_of, 'filed': filed, 'liabilities': r(liabilities, 0), 'equity': r(equity, 0),
            'debt_to_equity_pct': r(liabilities/equity*100, 1) if equity > 0 else None, 'note': DEBT_NOTE}


def valuation(price, eps_ttm, basis, history):
    """PER now and at past fiscal year ends; history: [{'year_end', 'eps', 'close'}] (split-adjusted)."""
    if price is None or eps_ttm is None:
        return None
    past = [{**h, 'eps': r(h['eps'], 4), 'close': r(h['close'], 4),
             'per': r(h['close']/h['eps'], 1) if h['eps'] > 0 and h['close'] else None} for h in history]
    pers = sorted(h['per'] for h in past if h['per'] is not None)
    per = r(price/eps_ttm, 1) if eps_ttm > 0 else None
    median = r(float(np.median(pers)), 1) if pers else None
    return {'price': r(price, 4), 'eps_ttm': r(eps_ttm, 4), 'eps_basis': basis, 'per_ttm': per,
            'loss_making': eps_ttm <= 0, 'per_history': past, 'per_history_median': median,
            'per_vs_history_median_pct': r((per/median-1)*100, 1) if per and median else None, 'note': PER_NOTE}


def us_fundamentals(con, cik, closes=None, events=()):
    tags = sorted({t for ts in US_TAGS.values() for t in ts})
    q = con.execute("select tag, start, \"end\", val, form, filed, accn from read_parquet(?) where cik = ? and tag in ("
                    + ','.join('?'*len(tags)) + ") and form in ('10-Q','10-K','10-Q/A','10-K/A')",
                    [str(DATA/'sec'/'facts'/'*.parquet'), cik, *tags]).df()
    if q.empty:
        return None
    q = q.dropna(subset=['start', 'end']).sort_values('filed').drop_duplicates(['tag', 'start', 'end'], keep='last')
    out, eps_q, eps_rows = {}, None, None
    for key, choices in US_TAGS.items():
        # Companies switch tags over the years (NVDA's revenue tag stopped in 2020): use the one with the newest period.
        present = [q[q['tag'] == t] for t in choices if (q['tag'] == t).any()]
        if not present:
            continue
        rows = max(present, key=lambda x: str(x['end'].max()))
        if key == 'eps_diluted':
            # Per-share numbers filed before a later split are put on today's share count, like the archive's prices.
            rows = rows.assign(val=[v/split_factor(events, f) for v, f in zip(rows['val'], rows['filed'])])
            eps_rows = rows
        qs = quarters(rows)
        if not qs:
            continue
        if key == 'eps_diluted':
            eps_q = qs
        last = qs[-1]
        end = pd.Timestamp(last['end'])
        prior = [x for x in qs if abs((pd.Timestamp(x['end'])-(end-timedelta(days=365))).days) <= 20]
        yoy = (last['value']/prior[-1]['value']-1)*100 if prior and prior[-1]['value'] > 0 else None
        out[key] = {'quarters': [{'end': x['end'], 'value': r(x['value'], 4), 'filed': x['filed'],
                                  **({'derived': '연간 − 1~3분기 누적'} if x['derived'] else {})} for x in qs[-4:]],
                    'latest_yoy_pct': r(yoy)}
    b = con.execute("select tag, \"end\", val, filed from read_parquet(?) where cik = ? and start is null and tag in ("
                    + ','.join('?'*len(US_BALANCE)) + ") and form in ('10-Q','10-K','10-Q/A','10-K/A')",
                    [str(DATA/'sec'/'facts'/'*.parquet'), cik, *US_BALANCE]).df()
    if not b.empty:
        b = b.sort_values('filed').drop_duplicates(['tag', 'end'], keep='last')
        end = b['end'].max()
        at = {t: float(v) for t, v in zip(b[b['end'] == end]['tag'], b[b['end'] == end]['val'])}
        equity = at.get('StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest', at.get('StockholdersEquity'))
        liabilities = at.get('Liabilities')
        if liabilities is None and equity is not None and 'LiabilitiesAndStockholdersEquity' in at:
            liabilities = at['LiabilitiesAndStockholdersEquity']-equity
        out['balance'] = debt(liabilities, equity, str(end), str(b[b['end'] == end]['filed'].max()))
    if eps_q and closes is not None and len(closes):
        years = eps_rows.assign(days=(pd.to_datetime(eps_rows['end'])-pd.to_datetime(eps_rows['start'])).dt.days)
        years = years[(years['days'] >= 350) & (years['days'] <= 380)].sort_values('end').tail(5)
        history = [{'year_end': str(y['end']), 'eps': float(y['val']), 'close': close_on(closes, y['end'])} for _, y in years.iterrows()]
        out['valuation'] = valuation(float(closes.iloc[-1]), ttm(eps_q), '최근 4분기 희석 주당순이익 합(SEC)', history)
    filings = con.execute("select form, filed from read_parquet(?) where cik = ? and form in ('10-Q','10-K') order by filed desc limit 1",
                          [str(DATA/'sec'/'filings'/'*.parquet'), cik]).fetchall()
    if filings:
        last = date.fromisoformat(str(filings[0][1])[:10])
        out['last_report'] = {'form': filings[0][0], 'filed': last.isoformat(),
                              'next_report_estimate': (last+timedelta(days=91)).isoformat(),
                              'note': '다음 실적 공시 예상일은 직전 10-Q/10-K 제출일 + 91일로 추정한 값이며 확정 일정이 아닙니다.'}
    return {'source': 'SEC EDGAR XBRL (filed = 공개된 날)', **out}


KR_ACCOUNTS = {'revenue': ('ifrs-full_Revenue',), 'operating_income': ('dart_OperatingIncomeLoss',), 'net_income': ('ifrs-full_ProfitLoss',)}
KR_EPS = 'ifrs-full_BasicEarningsLossPerShare'
REPORT_NAMES = {'11011': '사업보고서', '11012': '반기보고서', '11013': '1분기보고서', '11014': '3분기보고서'}
PERIOD_END = {'11013': '03-31', '11012': '06-30', '11014': '09-30', '11011': '12-31'}


def num(x):
    try:
        return float(str(x).replace(',', ''))
    except (TypeError, ValueError):
        return None


KR_EPS_PARTS = ('ifrs-full_BasicEarningsLossPerShareFromContinuingOperations',
                'ifrs-full_BasicEarningsLossPerShareFromDiscontinuedOperations')
EPS_COLUMNS = ('thstrm_amount', 'thstrm_add_amount', 'frmtrm_add_amount', 'frmtrm_q_amount')


def kr_eps_rows(con, slot, code):
    """The basic EPS row of one report, or - for a company that reports only continuing and discontinued operations (삼성
    바이오로직스 after its 2025 split-off) - their sum."""
    path = DATA/'dart'/'fin'/slot
    if not path.exists():
        return None
    rows = con.execute("select * from read_parquet(?) where stock_code = ? and account_id in (?, ?, ?)",
                       [str(path/'*.parquet'), code, KR_EPS, *KR_EPS_PARTS]).df()
    whole = rows[rows['account_id'] == KR_EPS]
    if len(whole):
        return whole.iloc[0]
    parts = [rows[rows['account_id'] == a] for a in KR_EPS_PARTS]
    if parts[0].empty:
        return None
    combined = parts[0].iloc[0].copy()
    for col in EPS_COLUMNS:
        if col in combined.index and num(combined[col]) is not None:
            combined[col] = num(combined[col])+sum(num(p.iloc[0].get(col)) or 0 for p in parts[1:] if len(p))
    return combined


def kr_fundamentals(con, code, closes=None, events=()):
    # Newest reporting period first: within a year annual (Dec) > Q3 (Sep) > half (Jun) > Q1 (Mar). A plain string sort put
    # the Q1 report (11013) ahead of the half-year report (11012).
    period = {'11013': 1, '11012': 2, '11014': 3, '11011': 4}
    folders = [p.name for p in (DATA/'dart'/'fin').glob('*_*') if p.is_dir()] if (DATA/'dart'/'fin').exists() else []
    slots = sorted((s for s in folders if s.split('_')[1] in period), key=lambda s: (int(s.split('_')[0]), period[s.split('_')[1]]), reverse=True)
    out = {'source': 'DART 오픈API (접수번호 앞 8자리 = 공시일)'}
    for slot in slots:
        rows = con.execute("select * from read_parquet(?) where stock_code = ? and sj_div in ('IS','CIS','BS')",
                           [str(DATA/'dart'/'fin'/slot/'*.parquet'), code]).df()
        if rows[rows['sj_div'].isin(['IS', 'CIS'])].empty:
            continue
        year, report = slot.split('_')
        filed = str(rows['rcept_no'].iloc[0])[:8]
        out['report'] = {'year': year, 'kind': REPORT_NAMES.get(report, report), 'filed': filed,
                         'statement': '연결' if rows['fs_div'].iloc[0] == 'CFS' else '별도'}
        for key, ids in KR_ACCOUNTS.items():
            hit = rows[rows['account_id'].isin(ids) & rows['sj_div'].isin(['IS', 'CIS'])]
            if hit.empty:
                continue
            x = hit.iloc[0]
            this, prior = num(x.get('thstrm_amount')), num(x.get('frmtrm_q_amount')) or num(x.get('frmtrm_amount'))
            out[key] = {'this_period': this, 'year_ago_period': prior,
                        'yoy_pct': r((this/prior-1)*100) if this is not None and prior and prior > 0 else None,
                        'cumulative': num(x.get('thstrm_add_amount'))}
        sheet = rows[rows['sj_div'] == 'BS']
        pick = lambda ident: next((num(v) for v in sheet[sheet['account_id'] == ident]['thstrm_amount']), None)
        out['balance'] = debt(pick('ifrs-full_Liabilities'), pick('ifrs-full_Equity'), f'{year}-{PERIOD_END[report]}',
                              f'{filed[:4]}-{filed[4:6]}-{filed[6:]}')
        if closes is not None and len(closes):
            out['valuation'] = kr_valuation(con, code, slots, year, report, closes, events)
        break
    since = (date.today()-timedelta(days=30)).strftime('%Y%m%d')
    if (DATA/'dart'/'list').exists():
        recent = con.execute("select rcept_dt, report_nm from read_parquet(?) where stock_code = ? and rcept_dt >= ? order by rcept_dt desc limit 15",
                             [str(DATA/'dart'/'list'/'*.parquet'), code, since]).fetchall()
        out['disclosures_30d'] = [{'date': d, 'title': str(t).strip()} for d, t in recent]
    return out if len(out) > 1 else None


def kr_valuation(con, code, slots, year, report, closes, events):
    """TTM basic EPS: the last annual figure, plus this year's cumulative, minus last year's same-period cumulative."""
    def adjusted(row, key):
        value = num(row.get(key))
        return None if value is None else value/split_factor(events, f"{str(row['rcept_no'])[:4]}-{str(row['rcept_no'])[4:6]}-{str(row['rcept_no'])[6:8]}")
    now = kr_eps_rows(con, f'{year}_{report}', code)
    if now is None:
        return None
    if report == '11011':
        eps = adjusted(now, 'thstrm_amount')
    else:
        annual = kr_eps_rows(con, f'{int(year)-1}_11011', code)
        this = adjusted(now, 'thstrm_add_amount') if num(now.get('thstrm_add_amount')) is not None else adjusted(now, 'thstrm_amount')
        before = adjusted(now, 'frmtrm_add_amount') if num(now.get('frmtrm_add_amount')) is not None else adjusted(now, 'frmtrm_q_amount')
        last_year = adjusted(annual, 'thstrm_amount') if annual is not None else None
        eps = last_year+this-before if None not in (last_year, this, before) else None
    history = []
    for slot in sorted(s for s in slots if s.endswith('_11011'))[-5:]:
        row = kr_eps_rows(con, slot, code)
        y = slot.split('_')[0]
        if row is not None and adjusted(row, 'thstrm_amount') is not None:
            history.append({'year_end': f'{y}-12-31', 'eps': adjusted(row, 'thstrm_amount'), 'close': close_on(closes, f'{y}-12-31')})
    return valuation(float(closes.iloc[-1]), eps, '직전 사업연도 기본 주당순이익 + 올해 누적 − 작년 같은 기간 누적(DART)', history)


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


def splits(path, market):
    """Split dates (exchange local date) and ratios (new shares per old share) of the last 400 days. The site uses them to
    keep a held position, a waiting plan and the scoring of a name right across a split."""
    from zoneinfo import ZoneInfo
    epath = path.parent/'events'/f'{path.stem}.json'
    try:
        items = (json.loads(epath.read_text()).get('splits') or {}).values()
    except (OSError, ValueError):
        return []
    zone = ZoneInfo('Asia/Seoul' if market == 'KR' else 'America/New_York')
    since = time.time()-400*86400
    out = []
    for x in items:
        try:
            ratio = float(x['numerator'])/float(x['denominator'])
            when = int(x['date'])
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        if when >= since and ratio > 0 and abs(ratio-1) > .01:
            out.append({'ex_date': datetime.fromtimestamp(when, zone).date().isoformat(), 'ratio': round(ratio, 6)})
    return sorted(out, key=lambda x: x['ex_date'])


def main():
    sys.path.insert(0, '/app')
    from app.instruments import SYMBOLS
    research = json.loads((DATA/'results'/'research.json').read_text())
    wide = DATA/'results'/'research-us-wide-signals.json'
    us_long = json.loads(wide.read_text()) if wide.exists() else None
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
                          '비용(수수료·세금·슬리피지) 반영. 청산 방식: 기본은 추적 손절(손절 1.5×ATR)·21거래일, 미국의 us_long·replay_us_long은 '
                          '손절 3×ATR·최대 63거래일(넓은 손절 실험). triggered_rules는 이 실험의 청산 방식에 맞는 값.',
                'signals_now': history['signals_now'], 'now': history['now'],
                'group_base_rates': group_rates(research, market, etf, us_long), 'own_history': history['rules'],
                'analogs': history.get('analogs'), 'regime': regimes[market], 'dividends': dividends(path, market), 'splits': splits(path, market)}
        try:
            prices = pd.read_parquet(path)[['date', 'close']].dropna()
            closes = prices.assign(date=prices['date'].astype(str)).set_index('date')['close'].sort_index()
            if not etf and market == 'US' and symbol in tickers:
                pack['fundamentals'] = us_fundamentals(con, int(tickers[symbol]), closes, split_events(path))
            elif not etf and market == 'KR':
                pack['fundamentals'] = kr_fundamentals(con, symbol, closes, split_events(path))
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
