"""Korean delisted companies (KRX KIND list, 2006-) and their daily history (Naver chart, which keeps delisted names), so the
backtest is not limited to the survivors. Output: DATA/universe/kr_delisted.parquet, DATA/daily/KR_DELISTED/{code}.parquet.
Days without trading (halts) come back with zero open/high/low; those take the close so a halt reads as a flat day."""
import os, re, time
from pathlib import Path
import pandas as pd
import requests

DATA = Path(os.environ.get('DATA', '/data'))
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) stock-lab-research'}


def listing(year):
    html = requests.post('https://kind.krx.co.kr/investwarn/delcompany.do', headers=UA, timeout=60, data={
        'method': 'searchDelCompanySub', 'forward': 'delcompany_down', 'currentPageSize': 5000, 'pageIndex': 1,
        'orderMode': 1, 'orderStat': 'D', 'marketType': '', 'searchCorpName': '',
        'fromDate': f'{year}-01-01', 'toDate': f'{year}-12-31', 'startDate': f'{year}-01-01', 'endDate': f'{year}-12-31'}
    ).content.decode('euc-kr', 'replace')
    out = []
    for row in re.findall(r'<tr>(.*?)</tr>', html, re.S):
        td = [re.sub(r'<[^>]+>', '', c).strip() for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
        if len(td) >= 5 and re.fullmatch(r'[0-9A-Z]{6}', td[2]):
            out.append({'symbol': td[2], 'name': td[1], 'delisted': td[3], 'reason': td[4]})
    return out


def history(code):
    text = requests.get('https://fchart.stock.naver.com/sise.nhn', headers=UA, timeout=60,
                        params={'symbol': code, 'timeframe': 'day', 'count': 8000, 'requestType': 0}).content.decode('euc-kr', 'replace')
    rows = [x.split('|') for x in re.findall(r'item data="([^"]*)"', text)]
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
    for k in ('open', 'high', 'low', 'close', 'volume'):
        df[k] = pd.to_numeric(df[k], errors='coerce')
    df['date'] = pd.to_datetime(df['date'], format='%Y%m%d').dt.strftime('%Y-%m-%d')
    for k in ('open', 'high', 'low'):
        df.loc[df[k] <= 0, k] = df['close']
    df['adjclose'] = df['close']
    return df[df['close'] > 0]


def main():
    rows = []
    for year in range(2006, 2027):
        rows += listing(year)
        time.sleep(.5)
    lst = pd.DataFrame(rows).drop_duplicates('symbol', keep='first')
    (DATA/'universe').mkdir(parents=True, exist_ok=True)
    lst.to_parquet(DATA/'universe'/'kr_delisted.parquet', index=False)
    out = DATA/'daily'/'KR_DELISTED'
    out.mkdir(parents=True, exist_ok=True)
    got = 0
    for r in lst.itertuples():
        path = out/f'{r.symbol}.parquet'
        if path.exists():
            got += 1
            continue
        time.sleep(.6)
        try:
            df = history(r.symbol)
        except requests.RequestException:
            continue
        if df is not None and len(df):
            df.to_parquet(path, index=False, compression='zstd')
            got += 1
    print(f'delisted: {len(lst)} listed, {got} with history', flush=True)


if __name__ == '__main__':
    main()
