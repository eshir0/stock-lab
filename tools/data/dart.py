"""DART (Korean FSS) archive: listed companies' financial statements and disclosure list, collected within the key's daily
quota (20,000 requests a day; this job stops at BUDGET and continues the next day).

The key is read from DART_KEY (root-only env file, never in the code). Output under DATA/dart:
  corps.parquet                       corp_code, corp_name, stock_code (listed companies only), modify_date
  list/{YYYY}Q{n}_{Y|K}.parquet       every disclosure of KOSPI (Y) / KOSDAQ (K) companies in that quarter
  fin/{YEAR}_{REPORT}/part-NNN.parquet  full financial statements (fnlttSinglAcntAll), consolidated when available,
                                       otherwise separate; rcept_no starts with the filing date (use it against look-ahead)
  progress.json                        what is done, today's request count

Order: the disclosure list first (cheap, ~2k requests a year), newest first; then the statements, newest year first,
annual -> half -> Q3 -> Q1. Statements exist from 2015.
"""
import datetime as dt
import io
import json
import os
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd
import requests

DATA = Path(os.environ.get('DATA', '/data'))/'dart'
API = 'https://opendart.fss.or.kr/api/'
BUDGET = int(os.environ.get('DART_BUDGET', '18000'))
FIRST_YEAR = 2015
REPORTS = {'11011': 'annual', '11012': 'half', '11014': 'q3', '11013': 'q1'}
# A report is not due before: annual -> end of March next year, half -> mid-August, Q3 -> mid-November, Q1 -> mid-May.
DUE = {'11011': (1, 4, 1), '11012': (0, 8, 20), '11014': (0, 11, 20), '11013': (0, 5, 20)}
PACE = .25


def log(*args):
    print(time.strftime('%H:%M:%S'), 'dart:', *args, flush=True)


class Quota(Exception):
    pass


class Client:
    def __init__(self, progress):
        key = os.environ.get('DART_KEY', '').strip()
        if len(key) != 40:
            raise SystemExit('DART_KEY is not set')
        self.key, self.p = key, progress
        today = dt.date.today().isoformat()
        if self.p.get('day') != today:
            self.p['day'], self.p['used'] = today, 0
        self.s = requests.Session()

    def get(self, path, **params):
        if self.p['used'] >= BUDGET:
            raise Quota(f'budget {BUDGET} used')
        time.sleep(PACE)
        wait = 5
        for _ in range(5):
            try:
                r = self.s.get(API+path, params={'crtfc_key': self.key, **params}, timeout=60)
            except requests.RequestException:
                time.sleep(wait); wait *= 2
                continue
            self.p['used'] += 1
            if r.status_code >= 500 or r.status_code == 429:
                time.sleep(wait); wait *= 2
                continue
            if path.endswith('.xml'):
                return r.content
            body = r.json()
            if body.get('status') == '020':
                raise Quota('DART daily limit reached')
            if body.get('status') in ('010', '011', '012', '901'):
                raise SystemExit(f'DART refused the key: {body.get("message")}')
            return body
        raise Quota('DART is not answering')


def write(df, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    df.to_parquet(tmp, index=False, compression='zstd')
    tmp.replace(path)


def corps(c):
    raw = zipfile.ZipFile(io.BytesIO(c.get('corpCode.xml'))).read('CORPCODE.xml')
    rows = [{k: (x.findtext(k) or '').strip() for k in ('corp_code', 'corp_name', 'stock_code', 'modify_date')}
            for x in ET.fromstring(raw).iter('list')]
    df = pd.DataFrame(rows)
    df = df[df['stock_code'] != ''].reset_index(drop=True)
    write(df, DATA/'corps.parquet')
    log(f'{len(df)} listed companies')
    return df


def quarters(today):
    y, q = today.year, (today.month-1)//3+1
    while y >= FIRST_YEAR:
        yield y, q
        q -= 1
        if q == 0:
            y, q = y-1, 4


def disclosures(c, today):
    done = c.p.setdefault('list', {})
    current = f'{today.year}Q{(today.month-1)//3+1}'
    for y, q in quarters(today):
        name = f'{y}Q{q}'
        start = dt.date(y, 3*q-2, 1)
        end = min(dt.date(y+(q == 4), (3*q) % 12+1, 1)-dt.timedelta(days=1), today)
        for cls in ('Y', 'K'):
            key = f'{name}_{cls}'
            if done.get(key) == 'done' and name != current:
                continue
            rows, page, pages = [], 1, 1
            while page <= pages:
                body = c.get('list.json', bgn_de=start.strftime('%Y%m%d'), end_de=end.strftime('%Y%m%d'), corp_cls=cls,
                             page_no=page, page_count=100)
                if body.get('status') == '013':
                    break
                pages = int(body.get('total_page') or 1)
                rows += body.get('list') or []
                page += 1
            if rows:
                write(pd.DataFrame(rows), DATA/'list'/f'{key}.parquet')
            done[key] = 'done'
            log(f'list {key}: {len(rows)}')


def due(year, report, today):
    extra, month, day = DUE[report]
    return today >= dt.date(year+extra, month, day)


def statements(c, corp_df, today):
    done = c.p.setdefault('fin', {})
    for year in range(today.year, FIRST_YEAR-1, -1):
        for report in REPORTS:
            if not due(year, report, today):
                continue
            slot = f'{year}_{report}'
            state = done.setdefault(slot, {'corps': [], 'parts': 0})
            finished = set(state['corps'])
            todo = [r for r in corp_df.itertuples() if r.corp_code not in finished]
            if not todo:
                continue
            rows, batch = [], []
            try:
                for r in todo:
                    got = []
                    for fs in ('CFS', 'OFS'):
                        body = c.get('fnlttSinglAcntAll.json', corp_code=r.corp_code, bsns_year=year, reprt_code=report, fs_div=fs)
                        if body.get('status') == '000':
                            got = [{**x, 'fs_div': fs, 'stock_code': r.stock_code} for x in body.get('list') or []]
                            break
                    rows += got
                    batch.append(r.corp_code)
                    if len(batch) >= 200:
                        flush(state, slot, rows, batch)
                        rows, batch = [], []
            finally:
                flush(state, slot, rows, batch)
                save(c.p)
            log(f'statements {slot}: {len(state["corps"])} companies done')


def flush(state, slot, rows, batch):
    if rows:
        write(pd.DataFrame(rows), DATA/'fin'/slot/f'part-{state["parts"]:03d}.parquet')
        state['parts'] += 1
    state['corps'] += batch


def load():
    try:
        return json.loads((DATA/'progress.json').read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save(p):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA/'progress.json.tmp'
    tmp.write_text(json.dumps(p))
    tmp.replace(DATA/'progress.json')


def main():
    p = load()
    c = Client(p)
    today = dt.date.today()
    try:
        corp_df = corps(c) if c.p.get('corps_day') != c.p['day'] else pd.read_parquet(DATA/'corps.parquet')
        c.p['corps_day'] = c.p['day']
        disclosures(c, today)
        save(p)
        statements(c, corp_df, today)
        log('all caught up')
    except Quota as exc:
        log(f'stopping for today: {exc}')
    finally:
        save(p)
        log(f'requests today: {p["used"]}')


if __name__ == '__main__':
    main()
