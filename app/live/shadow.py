"""Shadow mode: record the live order each simulated proposal WOULD have produced, and never send it.

For every paper proposal this builds the exact order intent live trading would use, runs it through the same
checks (limits, price band, no-shorting, halt state) and stores the outcome. It gives real evidence for go-live
review - how often the AI's orders fit the absolute limits, what would have blocked them, what protective
orders they would have needed - without touching any broker. The gate is created with the disabled stub and
only its read-only ``evaluate`` is used.
"""
from collections import Counter
from decimal import Decimal

from .broker import DisabledBroker
from .gate import OrderGate, local_date, new_book
from .intent import OrderIntent
from .limits import LimitUsage
from .protective import plan_protective_orders

MAX_SHADOW = 500
# Reasons that only say "live trading is not switched on yet"; shadow mode assumes it is.
STRUCTURAL = frozenset({'locked', 'no-broker', 'buy-not-allowed', 'sell-not-allowed'})


def _dec(value):
    return Decimal(str(value))


def shadow_usage(state, intent, now):
    """Usage of the hypothetical live account: earlier would-submit shadow orders count as if they had been sent."""
    today = local_date(now, intent.market)
    used = LimitUsage()
    for record in state.get('shadow_orders', []):
        if (record['currency'] == intent.currency and record['would_submit'] and record['side'] == 'BUY'
                and local_date(record['time'], record['market']) == today):
            used.orders_today += 1
            used.notional_today += _dec(record['notional'])
    held = int(state.get('positions', {}).get(intent.symbol, {}).get('quantity', 0))
    used.held_quantity = held
    used.position_notional = intent.reference_price*held
    day = state.get('risk_days', {}).get(intent.currency)
    if day:
        used.loss_today = max(Decimal(0), _dec(day['baseline'])-_dec(day['last_equity']))
    return used


def record_shadow(state, *, proposal, quote, instrument, config, source, now, sizing=None):
    """Append one shadow record for a proposal. Everything is computed before the single append at the end."""
    side = proposal['side']
    price = _dec(quote['ask'] if side == 'BUY' else quote['bid'])
    intent = OrderIntent(symbol=instrument['symbol'], market=instrument['market'], currency=instrument['currency'],
                         side=side, quantity=int(proposal['quantity']), order_type='LIMIT', limit_price=price,
                         reference_price=_dec(proposal['reference_price']), source=source,
                         run_id=str(proposal.get('run_id', '')))
    gate = OrderGate(dict(state.get('live') or new_book()), DisabledBroker(), config, b'shadow', clock=lambda: now)
    evaluation = gate.evaluate(intent, shadow_usage(state, intent, now))
    blocked_by = [code for code in evaluation.codes() if code not in STRUCTURAL]
    protective = None
    if side == 'BUY' and sizing and sizing.get('stop_price') and sizing.get('take_profit_price'):
        result = plan_protective_orders(
            symbol=intent.symbol, market=intent.market, currency=intent.currency, quantity=intent.quantity,
            stop_price=sizing['stop_price'], take_profit_price=sizing['take_profit_price'], last_price=price,
            expire_date=local_date(sizing.get('expires_at') or now, intent.market).isoformat())
        protective = result.plan.to_dict() if result.plan else {'problems': list(result.problems)}
    record = {'time': now, 'symbol': intent.symbol, 'market': intent.market, 'currency': intent.currency,
              'side': side, 'quantity': intent.quantity, 'order_type': intent.order_type,
              'limit_price': str(price), 'notional': str(intent.notional()), 'source': source,
              'proposal_id': proposal.get('id', ''), 'would_submit': not blocked_by, 'blocked_by': blocked_by,
              'suggested_quantity': evaluation.suggested_quantity, 'protective': protective,
              'sim_status': proposal.get('status', '')}
    records = state.setdefault('shadow_orders', [])
    records.append(record)
    del records[:-MAX_SHADOW]
    return record


def shadow_summary(state):
    records = state.get('shadow_orders', [])
    blocked = Counter(code for r in records for code in r['blocked_by'])
    sent = [r for r in records if r['would_submit']]
    return {'total': len(records), 'would_submit': len(sent), 'blocked': len(records)-len(sent),
            'blocked_by': dict(blocked.most_common(6)),
            'by_side': {side: sum(1 for r in records if r['side'] == side) for side in ('BUY', 'SELL')},
            'notional': {cur: str(sum((_dec(r['notional']) for r in sent if r['currency'] == cur), Decimal(0)))
                         for cur in ('KRW', 'USD')},
            'recent': records[-10:]}
