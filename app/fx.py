"""The USD account seen in won: the paper dollars were bought with won at the experiment's start, so their worth to the owner
moves with USD/KRW as well as with the stocks.

The rate comes from the history archive's 10-minute reader (tools/data/fx.py -> EVIDENCE_DIR/fx.json, Yahoo's KRW=X). A
conversion costs SPREAD_BPS each way (Toss Securities in business hours: 95% preference on a 1% spread = 0.05%), so the start
costs rate x (1 + spread) won per dollar and the current won value is what selling the dollars back would give,
rate x (1 - spread). This is a view of the same account, never a trade: no money moves between the currencies.
"""
import json
import time
from pathlib import Path

SPREAD_BPS = 5
MAX_AGE = 4*86400               # the market is shut over the weekend; an older rate is treated as missing


def read(folder, now=None):
    """{'rate', 'time'} or None."""
    now = time.time() if now is None else now
    if not folder:
        return None
    try:
        data = json.loads((Path(folder)/'fx.json').read_text(encoding='utf-8'))
        rate, at = float(data['rate']), float(data['time'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not 500 < rate < 5000 or not 0 <= now-at <= MAX_AGE:
        return None
    return {'rate': rate, 'time': at}


def start(seed_usd, reading, now):
    """What the dollars cost in won at the start (stored with the experiment)."""
    if not seed_usd or seed_usd <= 0:
        return None
    if not reading:
        return {'pending': True}
    rate = reading['rate']
    return {'start_rate': rate, 'start_time': now, 'spread_bps': SPREAD_BPS,
            'krw_cost': round(seed_usd*rate*(1+SPREAD_BPS/1e4))}


def view(record, seed_usd, equity_usd, reading):
    """The USD account in won now: cost, value, and how much of the difference came from the stocks and from the rate."""
    if not record or record.get('pending') or not reading or equity_usd is None:
        return None
    r0, r1, spread = record['start_rate'], reading['rate'], record.get('spread_bps', SPREAD_BPS)/1e4
    value = equity_usd*r1*(1-spread)
    cost = record['krw_cost']
    return {'start_rate': r0, 'rate': r1, 'rate_time': reading['time'], 'krw_cost': cost, 'krw_value': round(value),
            'krw_pnl': round(value-cost), 'return_pct': round((value/cost-1)*100, 3) if cost else None,
            'from_stocks_krw': round((equity_usd-seed_usd)*r0), 'from_rate_krw': round(equity_usd*(r1-r0)),
            'conversion_costs_krw': round(seed_usd*r0*spread+equity_usd*r1*spread)}
