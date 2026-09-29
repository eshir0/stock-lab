"""Instrument catalogue: the fixed lineup, plus a curated pool the daily focus list draws from.

Leverage describes the fund's daily target, never a multiplier on its quoted price
or paper-account profit. Catalogue entries are available instruments, not picks.

`INSTRUMENTS` is the fixed lineup (owner configuration + leveraged ETFs) that the "fixed" universe mode trades.
`POOL` adds large, liquid names so a daily focus list has something to choose from. The pool is a closed
list on purpose: neither the AI nor any data feed can add a symbol to it, and small or thinly traded stocks
are simply not in it. `SYMBOLS` covers both, so every catalogue name has verified metadata.
"""
from .config import INSTRUMENTS as CONFIG_INSTRUMENTS


LEVERAGED_ETFS = [
    {'symbol': '122630', 'name': 'KODEX 레버리지', 'market': 'KR', 'currency': 'KRW',
     'demo_base': 25000, 'leveraged_etf': True, 'leverage_factor': 2,
     'underlying': 'KOSPI 200',
     'catalog_source': 'https://kind.krx.co.kr/disclosure/etfisudetail.do?method=searchEtfIsuSummary&strIsurCd=12263'},
    {'symbol': 'TQQQ', 'name': 'ProShares UltraPro QQQ', 'market': 'US', 'currency': 'USD',
     'demo_base': 60, 'leveraged_etf': True, 'leverage_factor': 3,
     'underlying': 'Nasdaq-100',
     'catalog_source': 'https://www.proshares.com/our-etfs/leveraged-and-inverse/tqqq'},
    {'symbol': 'SQQQ', 'name': 'ProShares UltraPro Short QQQ', 'market': 'US', 'currency': 'USD',
     'demo_base': 40, 'leveraged_etf': True, 'leverage_factor': -3,
     'underlying': 'Nasdaq-100',
     'catalog_source': 'https://www.proshares.com/our-etfs/leveraged-and-inverse/sqqq'},
]

_catalog = {item['symbol']: item for item in LEVERAGED_ETFS}
INSTRUMENTS = []
for configured in CONFIG_INSTRUMENTS:
    defaults = {'leveraged_etf': False, 'leverage_factor': 1, 'underlying': '', 'catalog_source': ''}
    defaults.update(_catalog.get(configured['symbol'], {}))
    INSTRUMENTS.append({**defaults, **configured})
_existing = {item['symbol'] for item in INSTRUMENTS}
INSTRUMENTS.extend(dict(item) for item in LEVERAGED_ETFS if item['symbol'] not in _existing)


def _pool(market, currency, rows):
    """rows: (symbol, name, demo_base, kind, extra) with kind 'stock' | 'etf' | 'lev'."""
    items = []
    for symbol, name, base, kind, extra in rows:
        items.append({'symbol': symbol, 'name': name, 'market': market, 'currency': currency, 'demo_base': base,
                      'etf': kind in ('etf', 'lev'), 'leveraged_etf': kind == 'lev', 'leverage_factor': 1,
                      'underlying': '', 'catalog_source': '', 'pool': True, **extra})
    return items


POOL = (
    _pool('KR', 'KRW', [
        ('373220', 'LG에너지솔루션', 400000, 'stock', {}), ('207940', '삼성바이오로직스', 1000000, 'stock', {}),
        ('005380', '현대차', 250000, 'stock', {}), ('000270', '기아', 100000, 'stock', {}),
        ('035420', 'NAVER', 200000, 'stock', {}), ('005490', 'POSCO홀딩스', 380000, 'stock', {}),
        ('105560', 'KB금융', 80000, 'stock', {}), ('012450', '한화에어로스페이스', 800000, 'stock', {}),
        ('329180', 'HD현대중공업', 400000, 'stock', {}), ('034020', '두산에너빌리티', 30000, 'stock', {}),
        ('068270', '셀트리온', 180000, 'stock', {}), ('006400', '삼성SDI', 300000, 'stock', {}),
        ('069500', 'KODEX 200', 40000, 'etf', {'underlying': 'KOSPI 200'}),
        ('252670', 'KODEX 200선물인버스2X', 3000, 'lev', {'leverage_factor': -2, 'underlying': 'KOSPI 200'})])
    + _pool('US', 'USD', [
        ('NVDA', 'NVIDIA', 180, 'stock', {}), ('AMZN', 'Amazon', 220, 'stock', {}),
        ('GOOGL', 'Alphabet', 250, 'stock', {}), ('META', 'Meta Platforms', 700, 'stock', {}),
        ('TSLA', 'Tesla', 400, 'stock', {}), ('AVGO', 'Broadcom', 350, 'stock', {}),
        ('AMD', 'AMD', 200, 'stock', {}), ('NFLX', 'Netflix', 1200, 'stock', {}),
        ('JPM', 'JPMorgan Chase', 300, 'stock', {}), ('LLY', 'Eli Lilly', 800, 'stock', {}),
        ('PLTR', 'Palantir', 180, 'stock', {}), ('ORCL', 'Oracle', 250, 'stock', {}),
        ('QQQ', 'Invesco QQQ', 600, 'etf', {'underlying': 'Nasdaq-100'}),
        ('SPY', 'SPDR S&P 500', 660, 'etf', {'underlying': 'S&P 500'}),
        ('SMH', 'VanEck Semiconductor', 300, 'etf', {'underlying': 'MVIS US Listed Semiconductor 25'})]))

# Every name the daily focus list may consider: the fixed lineup first, then the pool (no duplicates).
CATALOGUE = list(INSTRUMENTS)
CATALOGUE.extend(dict(item) for item in POOL if item['symbol'] not in {c['symbol'] for c in CATALOGUE})
SYMBOLS = {item['symbol']: item for item in CATALOGUE}
