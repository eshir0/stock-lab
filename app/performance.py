"""Paper-account performance; cached marks stay visible but never advance drawdown."""
import math
import time
from decimal import Decimal, ROUND_HALF_UP

from datetime import datetime
from zoneinfo import ZoneInfo

from .instruments import SYMBOLS


CURRENCIES = ('KRW', 'USD')
ZONES = {'KRW': ZoneInfo('Asia/Seoul'), 'USD': ZoneInfo('America/New_York')}
DAILY_KEEP = 400          # days of end-of-day equity kept per currency (the minute history covers only about a week)


def _money(value):
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valuation(s, currency, now, quote_age):
    value = Decimal(str(s['cash'].get(currency, 0)))
    basis = Decimal('0')
    holdings_value = Decimal('0')
    fresh = True
    for symbol, pos in s['positions'].items():
        if SYMBOLS.get(symbol, {}).get('currency') != currency:
            continue
        qty = Decimal(str(pos['quantity']))          # fractional US shares are floats in the ledger
        if qty <= 0:
            continue
        q = s['quotes'].get(symbol, {})
        price, asof = q.get('last'), q.get('asof')
        valid_price = _number(price) and price > 0
        valid_identity = q.get('currency') == currency and q.get('symbol') == symbol and q.get('mode') == s['mode']
        valid_time = _number(asof) and -5 <= now-asof <= quote_age
        fresh = fresh and valid_price and valid_identity and valid_time
        # A stale mark is an explicitly labelled estimate; malformed/cross-currency
        # quotes must not contaminate even the displayed account estimate.
        mark = price if valid_price and valid_identity else pos['average']
        holdings_value += Decimal(str(mark))*qty
        basis += Decimal(str(pos.get('cost_basis', _money(Decimal(str(pos['average']))*qty))))
    return _money(value+holdings_value), _money(holdings_value-basis), bool(fresh)


def record_performance(s, now=None, quote_age=30, force=False):
    """Mutate a ledger inside its transaction, retaining peaks outside capped history."""
    now = time.time() if now is None else now
    tracking = s.setdefault('performance', {'measurement_started_at': now})
    tracking.setdefault('measurement_started_at', now)
    point = {'time': now, 'fresh': {}}
    for currency in CURRENCIES:
        value, _, fresh = _valuation(s, currency, now, quote_age)
        point[currency], point['fresh'][currency] = value, fresh
        metric = tracking.setdefault(currency, {'peak': None, 'max_drawdown_pct': 0, 'last_valuation_at': None})
        if fresh:
            peak = metric['peak']
            peak = value if peak is None else max(peak, value)
            drawdown = max(0.0, (peak-value)/peak*100) if peak > 0 else 0.0
            metric.update(peak=peak, max_drawdown_pct=max(metric['max_drawdown_pct'], drawdown), last_valuation_at=now)
            daily = metric.setdefault('daily', {})
            daily[datetime.fromtimestamp(now, ZONES[currency]).date().isoformat()] = value
            for day in sorted(daily)[:-DAILY_KEEP]:
                del daily[day]
    history = s.setdefault('history', [])
    if force or not history or now-history[-1]['time'] >= 60:
        history.append(point)
        s['history'] = history[-10000:]


def performance_summary(s, now=None, quote_age=30):
    """Return currency-separated accounting, without mutating persistent tracking."""
    now = time.time() if now is None else now
    tracking = s.get('performance', {})
    result = {}
    for currency in CURRENCIES:
        value, unrealized, fresh = _valuation(s, currency, now, quote_age)
        initial = s['initial'].get(currency, 0)
        profit = _money(Decimal(str(value))-Decimal(str(initial)))
        trades = [trade for trade in s['trades'] if trade.get('currency') == currency]
        realized = _money(sum((Decimal(str(t.get('realized', 0))) for t in trades), Decimal('0')))
        costs = _money(sum((Decimal(str(t.get('fee', 0))) for t in trades), Decimal('0')))
        metric = tracking.get(currency, {})
        result[currency] = {
            'initial': initial, 'equity': value, 'profit': profit,
            'return_pct': profit/initial*100 if initial > 0 else None,
            'realized': realized, 'unrealized': unrealized, 'costs': costs,
            'max_drawdown_pct': metric.get('max_drawdown_pct', 0),
            'peak': metric.get('peak'), 'valuation_fresh': fresh,
            'last_valuation_at': metric.get('last_valuation_at'),
            'drawdown_since': tracking.get('measurement_started_at'),
        }
    return result
