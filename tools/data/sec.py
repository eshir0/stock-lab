"""SEC EDGAR bulk data: every US filer's XBRL financial facts and filing list, refreshed weekly.

SEC asks automated clients to name a contact in the User-Agent; it is read from SEC_CONTACT (kept in a root-only env file,
never in the code) and the bulk files mean two requests a week, far below SEC's 10 requests per second.

Output under DATA/sec:
  raw/companyfacts.zip, raw/submissions.zip   latest bulk files (replaced each week)
  facts/part-NNN.parquet    cik, taxonomy, tag, unit, start, end, val, fy, fp, form, filed, accn, frame
                            ('filed' is the day the number became public: use it to avoid look-ahead in backtests)
  filings/part-NNN.parquet  cik, form, filed, report_date, accession, primary_document, items
  companies.parquet         cik, name, tickers, exchanges, sic, sic_description, state, fiscal_year_end
  tickers.parquet           ticker -> cik (SEC's own map)
"""
import io
import json
import os
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests

DATA = Path(os.environ.get('DATA', '/data'))
BULK = {'companyfacts': 'https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip',
        'submissions': 'https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip'}
EVERY = 7*86400
BATCH = 100                      # companies per parquet part


def log(*args):
    print(time.strftime('%H:%M:%S'), 'sec:', *args, flush=True)


def session():
    contact = os.environ.get('SEC_CONTACT', '').strip()
    if '@' not in contact:
        raise SystemExit('SEC_CONTACT is not set: SEC requires a contact in the User-Agent')
    s = requests.Session()
    s.headers.update({'User-Agent': f'Stock Lab research {contact}', 'Accept-Encoding': 'gzip, deflate'})
    return s


def download(s, name, url, raw):
    path = raw/f'{name}.zip'
    if path.exists() and time.time()-path.stat().st_mtime < 86400:
        return path                      # fetched today already (a rerun after a failure)
    tmp = raw/f'{name}.zip.part'
    with s.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, 'wb') as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.replace(path)
    log(f'{name}.zip {path.stat().st_size/1e9:.2f} GB')
    return path


def write_parts(frames, folder, n):
    folder.mkdir(parents=True, exist_ok=True)
    df = pd.concat(frames, ignore_index=True)
    tmp = folder/f'part-{n:03d}.tmp'
    df.to_parquet(tmp, index=False, compression='zstd')
    tmp.replace(folder/f'part-{n:03d}.parquet')
    return len(df)


def facts(zpath, out):
    staging = out/'facts.new'
    if staging.exists():
        for f in staging.iterdir():
            f.unlink()
    frames, part, rows, companies = [], 0, 0, 0
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():
            if not name.endswith('.json'):
                continue
            body = json.loads(z.read(name))
            try:
                cik = int(body.get('cik') or name[3:13])
            except ValueError:
                continue
            records = []
            for taxonomy, tags in (body.get('facts') or {}).items():
                for tag, item in tags.items():
                    for unit, values in (item.get('units') or {}).items():
                        for v in values:
                            records.append((cik, taxonomy, tag, unit, v.get('start'), v.get('end'), v.get('val'), v.get('fy'),
                                            v.get('fp'), v.get('form'), v.get('filed'), v.get('accn'), v.get('frame')))
            if records:
                df = pd.DataFrame(records, columns=['cik', 'taxonomy', 'tag', 'unit', 'start', 'end', 'val', 'fy', 'fp',
                                                    'form', 'filed', 'accn', 'frame'])
                df['val'] = pd.to_numeric(df['val'], errors='coerce')
                df['fy'] = pd.to_numeric(df['fy'], errors='coerce').astype('Int32')
                frames.append(df)
            companies += 1
            if companies % BATCH == 0 and frames:
                rows += write_parts(frames, staging, part)
                frames, part = [], part+1
                if part % 10 == 0:
                    log(f'facts: {companies} companies, {rows:,} rows')
    if frames:
        rows += write_parts(frames, staging, part)
    swap(staging, out/'facts')
    log(f'facts: {companies} companies, {rows:,} rows')


def submissions(zpath, out):
    staging = out/'filings.new'
    if staging.exists():
        for f in staging.iterdir():
            f.unlink()
    frames, meta, part, rows, n = [], [], 0, 0, 0
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():
            if not name.endswith('.json'):
                continue
            body = json.loads(z.read(name))
            recent = (body.get('filings') or {}).get('recent') if 'filings' in body else body
            cik = body.get('cik') or name.split('-')[0].replace('CIK', '').replace('.json', '')
            if 'filings' in body:
                meta.append({'cik': int(cik), 'name': body.get('name'), 'tickers': ','.join(body.get('tickers') or []),
                             'exchanges': ','.join(x or '' for x in body.get('exchanges') or []), 'sic': body.get('sic'),
                             'sic_description': body.get('sicDescription'), 'state': body.get('stateOfIncorporation'),
                             'fiscal_year_end': body.get('fiscalYearEnd'), 'category': body.get('category')})
            if recent and recent.get('accessionNumber'):
                df = pd.DataFrame({'cik': int(cik), 'form': recent.get('form'), 'filed': recent.get('filingDate'),
                                   'report_date': recent.get('reportDate'), 'accession': recent.get('accessionNumber'),
                                   'primary_document': recent.get('primaryDocument'), 'items': recent.get('items')})
                frames.append(df)
            n += 1
            if n % (BATCH*5) == 0 and frames:
                rows += write_parts(frames, staging, part)
                frames, part = [], part+1
    if frames:
        rows += write_parts(frames, staging, part)
    swap(staging, out/'filings')
    pd.DataFrame(meta).to_parquet(out/'companies.parquet', index=False, compression='zstd')
    log(f'filings: {n} files, {rows:,} rows, {len(meta)} companies')


def swap(staging, final):
    old = final.with_name(final.name+'.old')
    if old.exists():
        for f in old.iterdir():
            f.unlink()
        old.rmdir()
    if final.exists():
        final.rename(old)
    staging.rename(final)
    if old.exists():
        for f in old.iterdir():
            f.unlink()
        old.rmdir()


def update(force=False):
    out = DATA/'sec'
    raw = out/'raw'
    raw.mkdir(parents=True, exist_ok=True)
    stamp = out/'updated.json'
    done = json.loads(stamp.read_text()) if stamp.exists() else {}
    if not force and time.time()-done.get('at', 0) < EVERY:
        log('up to date')
        return
    s = session()
    r = s.get('https://www.sec.gov/files/company_tickers.json', timeout=60)
    r.raise_for_status()
    pd.DataFrame(r.json().values()).rename(columns={'cik_str': 'cik'}).to_parquet(out/'tickers.parquet', index=False)
    facts(download(s, 'companyfacts', BULK['companyfacts'], raw), out)
    submissions(download(s, 'submissions', BULK['submissions'], raw), out)
    stamp.write_text(json.dumps({'at': time.time()}))


if __name__ == '__main__':
    import sys
    update(force='--force' in sys.argv)
