"""Broker-side protective orders (structure and pure planning; nothing here talks to a broker).

Today the app watches prices and exits positions itself. If the server, the network or the process is down,
nothing protects an open position. A live mode should also register the stop and take-profit with the broker
as one conditional "one cancels the other" (OCO) order, and keep the app-side monitor as a second layer.

Limits of the broker's OCO order, which this plan cannot remove:
  * both legs are LIMIT orders after the trigger, so a fast gap through the stop limit can leave the position
    unsold; the stop limit is set a little below the trigger to improve the chance of filling
  * Korean conditional orders trigger only in the regular session
  * the order needs an expiry date: an unfilled protective order expires and must be re-registered
"""
from dataclasses import dataclass
from decimal import Decimal

STOP_SLIP_BPS = Decimal(50)
PRICE_TOLERANCE = Decimal('0.001')     # broker tick rounding must not cause endless replace loops

NOTES = ('OCO 조건주문은 두 조건 모두 지정가라서 손절가를 급격히 건너뛰면 체결되지 않을 수 있습니다. 앱의 손절 감시는 그대로 유지합니다.',
         '국내 조건주문은 정규장에서만 발동합니다.',
         '가격은 호가 단위로 내림해야 합니다(어댑터가 처리).',
         '만료일까지 조건이 충족되지 않으면 자동 만료되므로 다시 등록해야 합니다.')


@dataclass(frozen=True)
class ProtectivePlan:
    symbol: str
    market: str
    currency: str
    quantity: int
    expire_date: str
    take_profit_trigger: Decimal
    take_profit_limit: Decimal
    stop_trigger: Decimal
    stop_limit: Decimal
    notes: tuple = NOTES

    def to_dict(self):
        return {'symbol': self.symbol, 'market': self.market, 'currency': self.currency, 'quantity': self.quantity,
                'expire_date': self.expire_date, 'take_profit_trigger': str(self.take_profit_trigger),
                'take_profit_limit': str(self.take_profit_limit), 'stop_trigger': str(self.stop_trigger),
                'stop_limit': str(self.stop_limit)}


@dataclass(frozen=True)
class PlanResult:
    plan: ProtectivePlan
    problems: tuple = ()


@dataclass(frozen=True)
class Action:
    kind: str                  # create | replace | cancel
    symbol: str
    protective_id: str = None
    plan: ProtectivePlan = None
    reason: str = ''


def plan_protective_orders(*, symbol, market, currency, quantity, stop_price, take_profit_price, last_price,
                           expire_date, stop_slip_bps=STOP_SLIP_BPS):
    """One OCO plan for a long position, or no plan plus the reason. The broker requires
    take-profit trigger > current price > stop trigger; outside that the app-side monitor must exit instead."""
    problems = []
    stop, target, last = Decimal(str(stop_price)), Decimal(str(take_profit_price)), Decimal(str(last_price))
    if type(quantity) is not int or quantity <= 0:
        problems.append('수량은 1 이상의 정수여야 합니다.')
    if not (stop > 0 and target > 0 and last > 0):
        problems.append('손절·익절·현재 가격이 모두 필요합니다.')
    elif last <= stop:
        problems.append('현재가가 이미 손절가 이하입니다. 앱의 청산 감시가 처리해야 합니다.')
    elif last >= target:
        problems.append('현재가가 이미 익절가 이상입니다. 앱의 청산 감시가 처리해야 합니다.')
    elif not stop < last < target:
        problems.append('손절가 < 현재가 < 익절가 조건을 만족하지 않습니다.')
    if problems:
        return PlanResult(None, tuple(problems))
    return PlanResult(ProtectivePlan(
        symbol=symbol, market=market, currency=currency, quantity=quantity, expire_date=expire_date,
        take_profit_trigger=target, take_profit_limit=target,
        stop_trigger=stop, stop_limit=stop*(1-Decimal(stop_slip_bps)/10000)))


def _close(a, b):
    a, b = Decimal(str(a)), Decimal(str(b))
    return abs(a-b) <= max(abs(a), abs(b))*PRICE_TOLERANCE


def _same(plan, existing):
    return (Decimal(str(existing['quantity'])) == plan.quantity and existing['expire_date'] == plan.expire_date
            and _close(existing['stop_trigger'], plan.stop_trigger)
            and _close(existing['take_profit_trigger'], plan.take_profit_trigger))


def diff_protective(plans, existing):
    """Actions that turn the broker's open protective orders into the wanted state (desired-state sync).
    ``existing`` items: {id, symbol, status, quantity, expire_date, stop_trigger, take_profit_trigger}."""
    actions, wanted = [], {plan.symbol: plan for plan in plans}
    live = [item for item in existing if item.get('status') == 'OPEN']
    for symbol, plan in wanted.items():
        mine = [item for item in live if item['symbol'] == symbol]
        if not mine:
            actions.append(Action('create', symbol, plan=plan, reason='보유 포지션에 보호 주문이 없습니다.'))
            continue
        keep, extras = mine[0], mine[1:]
        if not _same(plan, keep):
            actions.append(Action('replace', symbol, keep['id'], plan, '수량·가격·만료일이 계획과 다릅니다.'))
        actions.extend(Action('cancel', symbol, item['id'], reason='같은 종목의 중복 보호 주문입니다.') for item in extras)
    for item in live:
        if item['symbol'] not in wanted:
            actions.append(Action('cancel', item['symbol'], item['id'], reason='보유하지 않은 종목의 보호 주문입니다.'))
    return actions
