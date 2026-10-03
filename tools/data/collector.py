"""Stock Lab history collector: keeps a private archive of market history for backtests, separate from the site.

Sources (all public, free, read at a polite pace; the archive stays on this server and is never published):
- Symbol lists: Nasdaq Trader symbol directory (US stocks and ETFs), KRX KIND listing (KOSPI, KOSDAQ), Naver ETF list (KR ETFs).
- Prices: Yahoo Finance chart endpoint (unofficial; personal research use). Daily bars for the whole history, hourly bars for
  the last 730 days and 1-minute bars for the last 7 days of the liquid names; the minute archive grows by one run per day.
- Macro series (indices, VIX, Treasury yields, the dollar, KRW, oil, gold, copper, bitcoin), also from Yahoo.
The site's Toss API is never used here: it allows one token, which belongs to the live site.

Layout under DATA (default /data):
  universe/{us,kr,macro}.parquet        what to collect
  daily/{US,KR,MACRO}/{SYMBOL}.parquet  date, open, high, low, close (split-adjusted), adjclose, volume, plus events/
  hourly/{US,KR}/{SYMBOL}.parquet       time (UTC), OHLCV
  minute/{US,KR}/{SYMBOL}/{YYYY-MM}.parquet
  manifest.json                         last fetch per job and symbol, failures

Usage: collector.py universe | daily [--full] | hourly | minute | status
"""
import argparse
import datetime as dt
import io
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

DATA = Path(os.environ.get('DATA', '/data'))
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) stock-lab-research'}
PACE = float(os.environ.get('PACE_SECONDS', '0.7'))       # seconds between Yahoo requests (plus jitter)
CHART = 'https://query1.finance.yahoo.com/v8/finance/chart/'
MACRO = {'^GSPC': 'S&P 500', '^NDX': 'Nasdaq-100', '^IXIC': 'Nasdaq Composite', '^DJI': 'Dow Jones', '^RUT': 'Russell 2000',
         '^VIX': 'VIX', '^IRX': '13-week T-bill', '^FVX': '5-year Treasury', '^TNX': '10-year Treasury', '^TYX': '30-year Treasury',
         '^KS11': 'KOSPI', '^KQ11': 'KOSDAQ', '^KS200': 'KOSPI 200', 'KRW=X': 'USD/KRW', 'DX-Y.NYB': 'US Dollar Index',
         'CL=F': 'WTI crude', 'GC=F': 'Gold', 'HG=F': 'Copper', 'BTC-USD': 'Bitcoin', '^N225': 'Nikkei 225', '^HSI': 'Hang Seng',
         '000001.SS': 'Shanghai Composite', '^STOXX50E': 'Euro Stoxx 50'}
HOURLY_US, HOURLY_KR = 1500, 600                          # liquid names that also get hourly and minute bars
MINUTE_US, MINUTE_KR = 800, 300


def log(*args):
    print(time.strftime('%H:%M:%S'), *args, flush=True)


# ---- manifest -----------------------------------------------------------------------------------------------------------------

def load_manifest():
    try:
        return json.loads((DATA/'manifest.json').read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_manifest(m):
    tmp = DATA/'manifest.json.tmp'
    tmp.write_text(json.dumps(m))
    tmp.replace(DATA/'manifest.json')


def write_parquet(df, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    df.to_parquet(tmp, index=False, compression='zstd')
    tmp.replace(path)


# ---- HTTP ---------------------------------------------------------------------------------------------------------------------

session = requests.Session()
session.headers.update(UA)


def get(url, params=None, tries=5):
    wait = 5
    for attempt in range(tries):
        try:
            r = session.get(url, params=params, timeout=40)
        except requests.RequestException as exc:
            log('network', type(exc).__name__, url[:80])
            time.sleep(wait)
            wait *= 2
            continue
        if r.status_code == 429 or r.status_code >= 500:
            log('slow down', r.status_code, f'waiting {wait}s')
            time.sleep(wait)
            wait = min(wait*2, 600)
            continue
        return r
    return None


def chart(symbol, **params):
    """One Yahoo chart call -> (bars DataFrame, events dict) or (None, reason)."""
    time.sleep(PACE+random.random()*PACE/2)
    r = get(CHART+requests.utils.quote(symbol, safe='^=.-'), params={**params, 'includePrePost': 'false', 'events': 'div,splits'})
    if r is None:
        return None, 'no answer'
    try:
        body = r.json()['chart']
    except ValueError:
        return None, f'HTTP {r.status_code}'
    if body.get('error') or not body.get('result'):
        return None, (body.get('error') or {}).get('code', 'empty')
    res = body['result'][0]
    stamps = res.get('timestamp') or []
    if not stamps:
        return None, 'no bars'
    q = res['indicators']['quote'][0]
    df = pd.DataFrame({'time': pd.to_datetime(stamps, unit='s', utc=True), 'open': q.get('open'), 'high': q.get('high'),
                       'low': q.get('low'), 'close': q.get('close'), 'volume': q.get('volume')})
    adj = (res['indicators'].get('adjclose') or [{}])[0].get('adjclose')
    if adj is not None:
        df['adjclose'] = adj
    df = df.dropna(subset=['close'])
    meta = res.get('meta') or {}
    df.attrs['meta'] = {k: meta.get(k) for k in ('currency', 'exchangeName', 'instrumentType', 'exchangeTimezoneName', 'firstTradeDate')}
    return df, res.get('events') or {}


# ---- universe -----------------------------------------------------------------------------------------------------------------

def universe():
    out = DATA/'universe'
    rows = []
    for url, sym_col, exch in (('https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt', 'Symbol', 'NASDAQ'),
                               ('https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt', 'ACT Symbol', None)):
        df = pd.read_csv(io.StringIO(get(url).text), sep='|')
        df = df[~df[sym_col].astype(str).str.startswith('File Creation')]
        df = df[df['Test Issue'] == 'N']
        for _, x in df.iterrows():
            symbol, name = str(x[sym_col]), str(x['Security Name'])
            if '$' in symbol or re.search(r'\b(Warrant|Warrants|Right|Rights|Unit|Units)\b', name):
                continue
            rows.append({'symbol': symbol, 'yahoo': symbol.replace('.', '-'), 'name': name,
                         'exchange': exch or {'N': 'NYSE', 'A': 'NYSE American', 'P': 'NYSE Arca', 'Z': 'Cboe BZX',
                                              'V': 'IEX'}.get(x['Exchange'], x['Exchange']),
                         'etf': x['ETF'] == 'Y'})
    us = pd.DataFrame(rows).drop_duplicates('yahoo')
    write_parquet(us, out/'us.parquet')
    rows = []
    for market, suffix, code in (('KOSPI', '.KS', 'stockMkt'), ('KOSDAQ', '.KQ', 'kosdaqMkt')):
        html = get('https://kind.krx.co.kr/corpgeneral/corpList.do',
                   {'method': 'download', 'searchType': 13, 'marketType': code}).content.decode('euc-kr', 'replace')
        for cells in re.findall(r'<tr>(.*?)</tr>', html, re.S):
            td = [re.sub(r'<[^>]+>', '', c).strip() for c in re.findall(r'<td[^>]*>(.*?)</td>', cells, re.S)]
            if len(td) >= 6 and re.fullmatch(r'[0-9A-Z]{6}', td[2]):
                rows.append({'symbol': td[2], 'yahoo': td[2]+suffix, 'name': td[0], 'exchange': market, 'etf': False,
                             'industry': td[3], 'listed': td[5]})
    etfs = get('https://finance.naver.com/api/sise/etfItemList.nhn').content.decode('euc-kr', 'replace')
    for x in json.loads(etfs)['result']['etfItemList']:
        rows.append({'symbol': x['itemcode'], 'yahoo': x['itemcode']+'.KS', 'name': x['itemname'], 'exchange': 'KOSPI',
                     'etf': True, 'industry': 'ETF', 'listed': ''})
    kr = pd.DataFrame(rows).drop_duplicates('yahoo')
    write_parquet(kr, out/'kr.parquet')
    write_parquet(pd.DataFrame([{'symbol': k, 'yahoo': k, 'name': v} for k, v in MACRO.items()]), out/'macro.parquet')
    log(f'universe: US {len(us)} (ETF {int(us.etf.sum())}) · KR {len(kr)} (ETF {int(kr.etf.sum())}) · macro {len(MACRO)}')


def naver_daily(code):
    """Korean daily bars from Naver's chart feed for names Yahoo does not carry (also keeps delisted history). Naver's
    prices are already adjusted, so adjclose = close. Days without trading come back with zero open/high/low."""
    if not re.fullmatch(r'[0-9A-Z]{6}', str(code)):
        return None
    time.sleep(PACE)
    r = get('https://fchart.stock.naver.com/sise.nhn', {'symbol': code, 'timeframe': 'day', 'count': 8000, 'requestType': 0})
    if r is None or r.status_code != 200:
        return None
    rows = [x.split('|') for x in re.findall(r'item data="([^"]*)"', r.content.decode('euc-kr', 'replace'))]
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
    for k in ('open', 'high', 'low', 'close', 'volume'):
        df[k] = pd.to_numeric(df[k], errors='coerce')
    df['date'] = pd.to_datetime(df['date'], format='%Y%m%d').dt.strftime('%Y-%m-%d')
    for k in ('open', 'high', 'low'):
        df.loc[df[k] <= 0, k] = df['close']
    df['adjclose'] = df['close']
    df = df[df['close'] > 0].reset_index(drop=True)
    df.attrs['meta'] = {'currency': 'KRW', 'source': 'naver'}
    return df if len(df) else None


def load_universe(name):
    df = pd.read_parquet(DATA/'universe'/f'{name}.parquet')
    limit = int(os.environ.get('LIMIT', '0'))      # for trial runs
    return df.head(limit) if limit else df


def safe(symbol):
    return re.sub(r'[^0-9A-Za-z._=^-]', '_', symbol)


# ---- daily --------------------------------------------------------------------------------------------------------------------

def daily(full=False):
    m = load_manifest()
    done = m.setdefault('daily', {})
    jobs = [('MACRO', r) for r in load_universe('macro').itertuples()]
    jobs += [('KR', r) for r in load_universe('kr').itertuples()]
    jobs += [('US', r) for r in load_universe('us').itertuples()]
    today = time.time()
    count, saved_at = 0, time.time()
    for market, r in jobs:
        key = f'{market}:{r.yahoo}'
        path = DATA/'daily'/market/f'{safe(r.yahoo)}.parquet'
        last = done.get(key, {})
        if not full and path.exists() and 'error' not in last and today-last.get('at', 0) < 18*3600:
            continue
        if not full and 'error' in last and today-last.get('tried', 0) < 6*3600:
            continue
        # A whole-history read once a month (dividend adjustments move adjclose back in time), otherwise the last month.
        whole = full or not path.exists() or today-last.get('full_at', 0) > 30*86400
        params = {'period1': 0, 'period2': int(today), 'interval': '1d'} if whole else {'range': '1mo', 'interval': '1d'}
        df, events = chart(r.yahoo, **params)
        if df is None and market == 'KR':
            df, events = naver_daily(r.symbol), {}                 # names Yahoo does not carry
            if df is None:
                events = 'not on Yahoo or Naver'
        if df is None:
            # 'at' stays the last SUCCESS (or absent), so a failed name is never counted or skipped as fresh.
            done[key] = {**{k: v for k, v in last.items() if k != 'error'}, 'tried': today, 'error': events}
        else:
            if 'time' in df:
                df['date'] = df['time'].dt.tz_convert(df.attrs['meta'].get('exchangeTimezoneName') or 'UTC').dt.date.astype(str)
                df = df.drop(columns='time')
            if not whole and path.exists():
                old = pd.read_parquet(path)
                df = pd.concat([old[~old['date'].isin(df['date'])], df]).sort_values('date')
            df = df.drop_duplicates('date', keep='last').reset_index(drop=True)
            write_parquet(df, path)
            if events:
                (DATA/'daily'/market/'events').mkdir(parents=True, exist_ok=True)
                old = {}
                epath = DATA/'daily'/market/'events'/f'{safe(r.yahoo)}.json'
                if epath.exists() and not whole:
                    old = json.loads(epath.read_text())
                for kind, items in events.items():
                    old.setdefault(kind, {}).update(items)
                epath.write_text(json.dumps(old))
            done[key] = {'at': today, 'rows': len(df), 'first': df['date'].iloc[0], 'last': df['date'].iloc[-1],
                         'full_at': today if whole else last.get('full_at', 0), 'meta': df.attrs.get('meta')}
        count += 1
        if count % 200 == 0 or time.time()-saved_at > 120:
            save_manifest(m)
            saved_at = time.time()
            log(f'daily: {count} read, now {key}')
    save_manifest(m)
    log(f'daily: done, {count} read')


# ---- liquid names -------------------------------------------------------------------------------------------------------------

def liquid(market, n):
    """The n most traded names of a market by median daily value over the last 60 sessions (needs the daily archive)."""
    rows = []
    for path in (DATA/'daily'/market).glob('*.parquet'):
        try:
            df = pd.read_parquet(path, columns=['close', 'volume']).tail(60)
        except Exception:
            continue
        if len(df) >= 40:
            rows.append((path.stem, float((df['close']*df['volume']).median())))
    rows.sort(key=lambda x: -x[1])
    return [s for s, _ in rows[:n]]


def intraday(job, interval, rng, n_us, n_kr):
    m = load_manifest()
    done = m.setdefault(job, {})
    now = time.time()
    count = 0
    for market, n in (('US', n_us), ('KR', n_kr)):
        for symbol in liquid(market, n):
            key = f'{market}:{symbol}'
            last = done.get(key, {})
            if 'error' not in last and now-last.get('at', 0) < 18*3600:
                continue
            if 'error' in last and now-last.get('tried', 0) < 6*3600:
                continue
            df, events = chart(symbol, range=rng, interval=interval)
            for shorter in (('365d', '90d') if df is None and job == 'hourly' else ()):
                df, events = chart(symbol, range=shorter, interval=interval)     # listed less than two years ago
                if df is not None:
                    break
            if df is None:
                done[key] = {**{k: v for k, v in last.items() if k != 'error'}, 'tried': now, 'error': events}
                continue
            df = df.drop(columns=[c for c in ('adjclose',) if c in df])
            if job == 'hourly':
                path = DATA/'hourly'/market/f'{symbol}.parquet'
                if path.exists():
                    df = pd.concat([pd.read_parquet(path), df])
                write_parquet(df.drop_duplicates('time', keep='last').sort_values('time'), path)
            else:
                df['month'] = df['time'].dt.strftime('%Y-%m')
                for month, part in df.groupby('month'):
                    path = DATA/'minute'/market/symbol/f'{month}.parquet'
                    part = part.drop(columns='month')
                    if path.exists():
                        part = pd.concat([pd.read_parquet(path), part])
                    write_parquet(part.drop_duplicates('time', keep='last').sort_values('time'), path)
            done[key] = {'at': now, 'rows': len(df)}
            count += 1
            if count % 100 == 0:
                save_manifest(m)
                log(f'{job}: {count} read, now {key}')
    save_manifest(m)
    log(f'{job}: done, {count} read')


def status():
    m = load_manifest()
    size = sum(f.stat().st_size for f in DATA.rglob('*') if f.is_file())
    print(f'archive {size/1e9:.2f} GB')
    for job in ('daily', 'hourly', 'minute'):
        items = m.get(job, {})
        ok = [v for v in items.values() if 'rows' in v and 'error' not in v]
        print(f'{job}: {len(ok)} symbols, {sum(v["rows"] for v in ok):,} rows, {len(items)-len(ok)} failed')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('job', choices=('universe', 'daily', 'hourly', 'minute', 'all', 'status'))
    p.add_argument('--full', action='store_true')
    a = p.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    if a.job in ('universe', 'all'):
        universe()
    if a.job in ('daily', 'all'):
        daily(a.full)
    if a.job in ('hourly', 'all'):
        intraday('hourly', '1h', '730d', HOURLY_US, HOURLY_KR)
    if a.job in ('minute', 'all'):
        intraday('minute', '1m', '7d', MINUTE_US, MINUTE_KR)
    if a.job == 'status':
        status()
