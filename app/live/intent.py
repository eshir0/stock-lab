"""The order the system wants to place, in one immutable, hashable, JSON-friendly shape."""
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

SIDES = ('BUY', 'SELL')
ORDER_TYPES = ('LIMIT', 'MARKET')
MARKETS = {'KR': 'KRW', 'US': 'USD'}


def client_order_id(digest, nonce):
    """36 characters of [a-z0-9]: the broker's idempotency key format. Reusing it replays, never duplicates."""
    return 'sl'+hashlib.sha256(f'{digest}|{nonce}'.encode()).hexdigest()[:34]


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    market: str
    currency: str
    side: str
    quantity: int
    order_type: str = 'LIMIT'
    limit_price: Decimal = Decimal(0)
    reference_price: Decimal = Decimal(0)
    time_in_force: str = 'DAY'
    source: str = 'ai'
    run_id: str = ''

    def problems(self):
        found = []
        if not self.symbol or not self.symbol.replace('.', '').replace('-', '').isalnum():
            found.append('종목 코드가 올바르지 않습니다.')
        if MARKETS.get(self.market) != self.currency:
            found.append('시장과 통화가 일치하지 않습니다.')
        if self.side not in SIDES:
            found.append('매수/매도 구분이 올바르지 않습니다.')
        if type(self.quantity) is not int or self.quantity <= 0:
            found.append('수량은 1 이상의 정수여야 합니다.')
        if self.order_type not in ORDER_TYPES:
            found.append('주문 유형이 올바르지 않습니다.')
        if self.order_type == 'LIMIT' and not (self.limit_price.is_finite() and self.limit_price > 0):
            found.append('지정가 주문에는 0보다 큰 가격이 필요합니다.')
        if not (self.reference_price.is_finite() and self.reference_price > 0):
            found.append('기준 가격이 필요합니다.')
        return found

    def price(self):
        return self.limit_price if self.order_type == 'LIMIT' else self.reference_price

    def notional(self):
        return self.price()*self.quantity

    def to_dict(self):
        return {'symbol': self.symbol, 'market': self.market, 'currency': self.currency, 'side': self.side,
                'quantity': self.quantity, 'order_type': self.order_type, 'limit_price': str(self.limit_price),
                'reference_price': str(self.reference_price), 'time_in_force': self.time_in_force,
                'source': self.source, 'run_id': self.run_id}

    @classmethod
    def from_dict(cls, data):
        return cls(symbol=data['symbol'], market=data['market'], currency=data['currency'], side=data['side'],
                   quantity=data['quantity'], order_type=data['order_type'],
                   limit_price=Decimal(data['limit_price']), reference_price=Decimal(data['reference_price']),
                   time_in_force=data.get('time_in_force', 'DAY'), source=data.get('source', 'ai'),
                   run_id=data.get('run_id', ''))

    def digest(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()
