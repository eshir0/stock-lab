"""A small documented ETF catalogue, merged without rewriting owner configuration.

Leverage describes the fund's daily target, never a multiplier on its quoted price
or paper-account profit. Catalogue entries are available instruments, not picks.
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
SYMBOLS = {item['symbol']: item for item in INSTRUMENTS}
