"""Broker interface for a future live mode, and the only implementation this build has: a disabled stub.

``DisabledBroker`` refuses every call - reads included - so no account data is ever fetched and no order is
ever sent. A real adapter (Toss's official trading API) does not exist yet; adding one is the go-live work
described in docs/LIVE_TRADING.md, and it must also update tests/test_no_live_trading.py on purpose.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal

from .errors import LiveTradingLocked


@dataclass(frozen=True)
class BrokerOrder:
    """An order as the broker reports it, normalised."""
    order_id: str
    symbol: str
    side: str
    quantity: Decimal
    filled_quantity: Decimal = Decimal(0)
    limit_price: Decimal = None          # None for market orders
    status: str = ''                     # the broker's own status string
    ordered_at: float = 0.0
    average_fill_price: Decimal = None


@dataclass(frozen=True)
class BrokerSnapshot:
    """Everything reconciliation needs, read from the broker at one moment."""
    taken_at: float
    cash: dict = field(default_factory=dict)              # currency -> amount
    positions: dict = field(default_factory=dict)         # symbol -> quantity
    open_orders: tuple = ()
    closed_orders: tuple = ()                             # recent history, for matching finished orders


class Broker(ABC):
    name = 'abstract'
    can_trade = False

    @abstractmethod
    def normalize_price(self, symbol, price, side):
        """Round to the market's tick size. Tick tables belong to the adapter, not to the gate."""

    @abstractmethod
    def place_order(self, intent, client_order_id):
        """Send one order. MUST pass ``client_order_id`` as the idempotency key. Raises BrokerRejected when
        the broker definitively refused, BrokerUncertain when the outcome is unknown."""

    @abstractmethod
    def cancel_order(self, order_id):
        """Request cancellation of an open order."""

    @abstractmethod
    def find_orders(self, symbol):
        """Open orders plus recent closed ones for a symbol."""

    @abstractmethod
    def snapshot(self):
        """A BrokerSnapshot of cash, positions and orders."""

    @abstractmethod
    def list_protective(self):
        """Broker-side conditional (stop / take-profit) orders currently registered."""

    @abstractmethod
    def place_protective(self, plan, client_order_id):
        """Register a broker-side conditional order for a ProtectivePlan."""

    @abstractmethod
    def cancel_protective(self, protective_id):
        """Remove a broker-side conditional order."""


class DisabledBroker(Broker):
    name = 'disabled'
    can_trade = False

    def __init__(self, reason='실거래 주문 어댑터가 없습니다.'):
        self.reason = reason

    def _locked(self, *args, **kwargs):
        raise LiveTradingLocked(self.reason)

    normalize_price = place_order = cancel_order = find_orders = snapshot = _locked
    list_protective = place_protective = cancel_protective = _locked


def create_broker(config):
    """The app's only broker factory. Every path returns the disabled stub in this build."""
    return DisabledBroker(config.reason)


def orders_match(intent, submitted_at, order, now, skew=60):
    """Best-effort match of a broker order to one we tried to send (the broker's order records carry no
    client id). Same symbol, side, quantity and price, created no earlier than we submitted."""
    if order.symbol != intent.symbol or order.side != intent.side or order.quantity != Decimal(intent.quantity):
        return False
    if intent.order_type == 'LIMIT' and order.limit_price != intent.limit_price:
        return False
    if intent.order_type == 'MARKET' and order.limit_price is not None:
        return False
    return submitted_at-skew <= order.ordered_at <= now+skew
