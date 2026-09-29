"""Ledger-vs-broker reconciliation (structure and pure logic; nothing here reads a real account).

In live mode the broker is the source of truth for cash, positions and orders. The app's own ledger is a
mirror, and this check proves the mirror is right against a BrokerSnapshot. A wrong position is critical:
trading stops until a person resolves it.
"""
from dataclasses import dataclass
from decimal import Decimal

from . import lifecycle as st
from .broker import orders_match
from .intent import OrderIntent

CASH_TOLERANCE_PCT = Decimal('0.5')      # small drift from fees and rounding is expected
CASH_CRITICAL_PCT = Decimal('5')
CASH_TOLERANCE_ABS = {'KRW': Decimal(1000), 'USD': Decimal(1)}


@dataclass(frozen=True)
class Diff:
    kind: str
    subject: str
    expected: str
    actual: str
    severity: str           # 'warning' | 'critical'
    message: str


@dataclass(frozen=True)
class ReconcileReport:
    diffs: tuple

    @property
    def ok(self):
        return not self.diffs

    @property
    def halt(self):
        return any(diff.severity == 'critical' for diff in self.diffs)


def _dec(value):
    return Decimal(str(value))


def reconcile(ledger_positions, ledger_cash, gate_orders, snapshot, now):
    """Compare the app's ledger and open orders with what the broker reports at one moment."""
    diffs = []
    for symbol in sorted(set(ledger_positions)|set(snapshot.positions)):
        expected, actual = _dec(ledger_positions.get(symbol, 0)), _dec(snapshot.positions.get(symbol, 0))
        if expected != actual:
            diffs.append(Diff('position-mismatch', symbol, str(expected), str(actual), 'critical',
                              f'{symbol} 보유 수량이 장부 {expected}주, 증권사 {actual}주로 다릅니다.'))
    for currency in sorted(set(ledger_cash)|set(snapshot.cash)):
        expected, actual = _dec(ledger_cash.get(currency, 0)), _dec(snapshot.cash.get(currency, 0))
        gap = abs(expected-actual)
        floor = CASH_TOLERANCE_ABS.get(currency, Decimal(0))
        if gap > max(floor, abs(expected)*CASH_TOLERANCE_PCT/100):
            critical = gap > max(floor, abs(expected)*CASH_CRITICAL_PCT/100)
            diffs.append(Diff('cash-mismatch', currency, str(expected), str(actual),
                              'critical' if critical else 'warning',
                              f'{currency} 현금이 장부 {expected:,.2f}, 증권사 {actual:,.2f}로 {gap:,.2f} 다릅니다.'))
    seen = set()
    everything = tuple(snapshot.open_orders)+tuple(snapshot.closed_orders)
    for record in gate_orders:
        if record['state'] not in st.LIVE_EXPOSURE:
            continue
        intent = OrderIntent.from_dict(record['intent'])
        match = next((o for o in everything if orders_match(intent, record['submitted_at'], o, now)
                      and o.order_id not in seen), None)
        if match is None:
            # An UNKNOWN order may simply not be visible yet; the gate already halts on it.
            severity = 'warning' if record['state'] in (st.UNKNOWN, st.SUBMITTING) else 'critical'
            diffs.append(Diff('order-missing', record['client_order_id'], record['state'], '없음', severity,
                              f'{intent.symbol} {intent.side} 주문이 증권사 내역에서 확인되지 않습니다.'))
            continue
        seen.add(match.order_id)
        mapped = st.map_broker_status(match.status)
        if mapped != record['state']:
            diffs.append(Diff('order-state-drift', record['client_order_id'], record['state'], mapped, 'warning',
                              f'{intent.symbol} 주문 상태가 장부 {record["state"]}, 증권사 {mapped}로 다릅니다.'))
    for order in snapshot.open_orders:
        if order.order_id not in seen and not any(r.get('order_id') == order.order_id for r in gate_orders):
            diffs.append(Diff('external-order', order.order_id, '없음', order.status, 'warning',
                              f'{order.symbol} {order.side} 미체결 주문이 이 앱이 보낸 것이 아닙니다(앱·웹에서 직접 낸 주문일 수 있음).'))
    return ReconcileReport(tuple(diffs))
