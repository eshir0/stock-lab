"""USD/KRW for the site, every 10 minutes (stocklab-fx.timer): Yahoo's KRW=X quote written to DATA/evidence/fx.json, the
folder the app already mounts read-only. Yahoo's rate is the market mid-rate, close to the bank's reference rate the
broker converts at (the conversion spread is applied by the app). A failed read leaves the last file in place."""
import json
import os
import time
from pathlib import Path

import requests

DATA = Path(os.environ.get('DATA', '/data'))
URL = 'https://query1.finance.yahoo.com/v8/finance/chart/KRW=X'


def main():
    r = requests.get(URL, params={'range': '1d', 'interval': '5m'}, timeout=30,
                     headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) stock-lab-research'})
    r.raise_for_status()
    res = r.json()['chart']['result'][0]
    meta = res.get('meta') or {}
    rate, at = meta.get('regularMarketPrice'), meta.get('regularMarketTime')
    if not isinstance(rate, (int, float)) or not 500 < rate < 5000 or not at:
        raise SystemExit('unexpected USD/KRW reading')
    out = DATA/'evidence'
    out.mkdir(parents=True, exist_ok=True)
    tmp = out/'fx.json.tmp'
    tmp.write_text(json.dumps({'pair': 'USDKRW', 'rate': round(float(rate), 4), 'time': int(at), 'fetched': int(time.time()),
                               'source': 'Yahoo Finance KRW=X'}))
    tmp.replace(out/'fx.json')
    print('USD/KRW', rate)


if __name__ == '__main__':
    main()
