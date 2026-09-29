"""Absolute (currency-amount) limits that sit above the percentage-based risk rules.

The simulation sizes orders as a share of paper equity. With real money a bug, a bad price or a
runaway loop must hit a hard ceiling in plain currency amounts regardless of what the sizing maths
says. Every check here can only refuse an order; none can create or enlarge one.
"""
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# Conservative placeholders: they only restrict, and must be reviewed against real capital before go-live.
# Leveraged ETFs are deliberately absent from the default allow-list.
DEFAULT_SYMBOLS = ('005930', '000660', 'AAPL', 'MSFT')


@dataclass(frozen=True)
class CurrencyLimits:
    max_order: Decimal
    max_daily_notional: Decimal
    max_daily_orders: int
    max_daily_loss: Decimal
    max_position: Decimal

    def public(self):
        return {'max_order': str(self.max_order), 'max_daily_notional': str(self.max_daily_notional),
                'max_daily_orders': self.max_daily_orders, 'max_daily_loss': str(self.max_daily_loss),
                'max_position': str(self.max_position)}


@dataclass(frozen=True)
class LiveLimits:
    KRW: CurrencyLimits
    USD: CurrencyLimits
    symbols: frozenset
    max_open_orders: int = 3
    price_band_pct: Decimal = Decimal('1')

    def for_currency(self, currency):
        return {'KRW': self.KRW, 'USD': self.USD}[currency]

    @classmethod
    def default(cls):
        return cls(KRW=CurrencyLimits(Decimal(100000), Decimal(300000), 10, Decimal(30000), Decimal(200000)),
                   USD=CurrencyLimits(Decimal(100), Decimal(300), 10, Decimal(30), Decimal(200)),
                   symbols=frozenset(DEFAULT_SYMBOLS))

    @classmethod
    def from_env(cls, env):
        base = cls.default()

        def number(name, default, integer=False):
            raw = env.get(name)
            if raw is None or str(raw).strip() == '':
                return default
            try:
                value = Decimal(str(raw).strip())
            except InvalidOperation:
                raise ValueError(f'{name} 값이 숫자가 아닙니다.') from None
            if not value.is_finite() or value < 0 or (integer and value != value.to_integral_value()):
                raise ValueError(f'{name} 값은 0 이상의 {"정수" if integer else "숫자"}여야 합니다.')
            return int(value) if integer else value

        def currency(code, current):
            return CurrencyLimits(
                max_order=number(f'LIVE_MAX_ORDER_{code}', current.max_order),
                max_daily_notional=number(f'LIVE_MAX_DAILY_NOTIONAL_{code}', current.max_daily_notional),
                max_daily_orders=number('LIVE_MAX_DAILY_ORDERS', current.max_daily_orders, integer=True),
                max_daily_loss=number(f'LIVE_MAX_DAILY_LOSS_{code}', current.max_daily_loss),
                max_position=number(f'LIVE_MAX_POSITION_{code}', current.max_position))
        raw_symbols = env.get('LIVE_ALLOWED_SYMBOLS')
        symbols = base.symbols if raw_symbols is None else frozenset(
            x.strip().upper() for x in str(raw_symbols).split(',') if x.strip())
        return cls(KRW=currency('KRW', base.KRW), USD=currency('USD', base.USD), symbols=symbols,
                   max_open_orders=number('LIVE_MAX_OPEN_ORDERS', base.max_open_orders, integer=True),
                   price_band_pct=number('LIVE_PRICE_BAND_PCT', base.price_band_pct))

    def public(self):
        return {'KRW': self.KRW.public(), 'USD': self.USD.public(), 'symbols': sorted(self.symbols),
                'max_open_orders': self.max_open_orders, 'price_band_pct': str(self.price_band_pct)}


@dataclass
class LimitUsage:
    """What has already been used today (per currency) when a new order is checked."""
    orders_today: int = 0
    notional_today: Decimal = Decimal(0)
    open_orders: int = 0
    position_notional: Decimal = Decimal(0)   # current notional in the order's symbol
    loss_today: Decimal = Decimal(0)          # currency amount lost today, never negative
    held_quantity: int = None                 # shares currently held in the symbol (None = unknown)


@dataclass(frozen=True)
class Violation:
    code: str
    message: str


def check_limits(intent, usage, limits):
    """Every violated limit (empty list = within limits).

    Buys face every limit. Sells are exits and must never be trapped by size or count limits, so they only face
    two sanity checks: the price band, and never selling more than is held (no shorting).
    """
    found = []
    caps = limits.for_currency(intent.currency)
    notional = intent.notional()
    if intent.side == 'BUY':
        if intent.symbol not in limits.symbols:
            found.append(Violation('symbol-not-allowed', f'{intent.symbol}은(는) 실거래 허용 종목 목록에 없습니다.'))
        if notional > caps.max_order:
            found.append(Violation('max-order', f'주문 금액 {notional:,.2f}이(가) 1회 한도 {caps.max_order:,}을(를) 넘습니다.'))
        if usage.orders_today+1 > caps.max_daily_orders:
            found.append(Violation('max-daily-orders', f'하루 주문 횟수 한도 {caps.max_daily_orders}회를 넘습니다.'))
        if usage.notional_today+notional > caps.max_daily_notional:
            found.append(Violation('max-daily-notional', f'하루 누적 주문 금액이 한도 {caps.max_daily_notional:,}을(를) 넘습니다.'))
        if usage.open_orders+1 > limits.max_open_orders:
            found.append(Violation('max-open-orders', f'미체결 주문 한도 {limits.max_open_orders}건을 넘습니다.'))
        if usage.position_notional+notional > caps.max_position:
            found.append(Violation('max-position', f'종목 보유 금액이 한도 {caps.max_position:,}을(를) 넘습니다.'))
        if usage.loss_today >= caps.max_daily_loss:
            found.append(Violation('daily-loss-halt', f'오늘 손실이 한도 {caps.max_daily_loss:,}에 도달해 신규 매수를 막습니다.'))
    elif usage.held_quantity is not None and intent.quantity > usage.held_quantity:
        found.append(Violation('exceeds-position', f'보유 수량 {usage.held_quantity}주보다 많이 팔 수 없습니다(공매도 금지).'))
    if intent.order_type == 'LIMIT' and intent.reference_price > 0:
        drift = abs(intent.limit_price/intent.reference_price-1)*100
        if drift > limits.price_band_pct:
            found.append(Violation('price-band', f'지정가가 기준가에서 {drift:.2f}% 벗어나 착오 방지 범위 {limits.price_band_pct}%를 넘습니다.'))
    return found


def max_allowed_quantity(intent, usage, limits):
    """Largest whole quantity that would pass the amount limits (0 when none would)."""
    if intent.side == 'SELL':
        return intent.quantity if usage.held_quantity is None else max(0, min(intent.quantity, usage.held_quantity))
    price = intent.price()
    caps = limits.for_currency(intent.currency)
    if price <= 0 or intent.symbol not in limits.symbols or usage.loss_today >= caps.max_daily_loss:
        return 0
    room = min(caps.max_order, caps.max_daily_notional-usage.notional_today, caps.max_position-usage.position_notional)
    if room <= 0 or usage.orders_today+1 > caps.max_daily_orders or usage.open_orders+1 > limits.max_open_orders:
        return 0
    return max(0, min(intent.quantity, math.floor(room/price)))
