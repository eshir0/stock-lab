"""Order gate: the only door an order may go through. preview -> confirmation token -> execute.

Rules (see docs/LIVE_TRADING.md):
  * a blocked preview never gets a token; a token is bound to one exact order, expires, and works once
  * every check runs again at execute time, and the token is burned BEFORE the broker is called
  * the broker always receives a client order id (its idempotency key); an unclear outcome becomes UNKNOWN
    and is never guessed away
  * while any order is UNKNOWN nothing new is sent (the position is not known)

The gate keeps its state in a plain JSON-friendly ``book`` dict so the caller can persist it with the rest
of the ledger; it never talks to storage or the network itself, only to the injected broker.
"""
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import lifecycle as st
from .broker import orders_match
from .errors import BrokerRejected, BrokerUncertain, InvalidConfirmation, LiveTradingLocked, PolicyBlocked
from .intent import OrderIntent, client_order_id
from .limits import LimitUsage, check_limits, max_allowed_quantity

TOKEN_TTL = 120        # seconds a confirmation token stays valid
RETRY_WINDOW = 480     # the broker replays an idempotency key for 10 minutes; stay well inside it
MAX_PREVIEWS = 50
ZONES = {'KR': 'Asia/Seoul', 'US': 'America/New_York'}


def new_book():
    return {'orders': {}, 'previews': {},
            'halt': {'active': False, 'manual': False, 'reason': '', 'since': 0}}


@dataclass(frozen=True)
class Reason:
    code: str
    message: str


@dataclass(frozen=True)
class Evaluation:
    reasons: tuple
    suggested_quantity: int

    @property
    def blocked(self):
        return bool(self.reasons)

    def codes(self):
        return [reason.code for reason in self.reasons]


@dataclass(frozen=True)
class Preview:
    ok: bool
    evaluation: Evaluation
    intent: OrderIntent
    token: str = None
    client_order_id: str = None
    expires_at: int = None


@dataclass(frozen=True)
class Submission:
    client_order_id: str
    intent: OrderIntent


@dataclass(frozen=True)
class Resolution:
    action: str      # not-unknown | adopted | ambiguous | retry-allowed | manual
    detail: str = ''


def local_date(timestamp, market):
    return datetime.fromtimestamp(timestamp, ZoneInfo(ZONES[market])).date()


def usage_from_book(book, intent, now, position_notional=Decimal(0), loss_today=Decimal(0), held_quantity=None):
    """Today's live usage for one currency, from the gate's own order records."""
    today = local_date(now, intent.market)
    used = LimitUsage(position_notional=position_notional, loss_today=loss_today, held_quantity=held_quantity)
    for record in book['orders'].values():
        other = OrderIntent.from_dict(record['intent'])
        if other.currency != intent.currency:
            continue
        state = record['state']
        if state in st.LIVE_EXPOSURE:
            used.open_orders += 1
        if state in (st.REJECTED, st.NOT_PLACED) or (state == st.CANCELED and not record['filled_quantity']):
            continue
        if local_date(record['submitted_at'], other.market) == today:
            used.orders_today += 1
            used.notional_today += other.notional()
    return used


class OrderGate:
    def __init__(self, book, broker, config, secret, clock=time.time):
        self.book, self.broker, self.config, self.secret, self.clock = book, broker, config, secret, clock
        for key, value in new_book().items():
            book.setdefault(key, value)

    # ---- checks -----------------------------------------------------------------------------------
    def unresolved(self):
        return [cid for cid, record in self.book['orders'].items() if record['state'] == st.UNKNOWN]

    def evaluate(self, intent, usage):
        """Every reason this order cannot go out right now. Pure: no state changes, no broker calls."""
        reasons = []
        problems = intent.problems()
        if problems:
            reasons.append(Reason('invalid-intent', ' '.join(problems)))
        if not self.config.enabled:
            reasons.append(Reason('locked', self.config.reason))
        if intent.side == 'BUY' and not self.config.allow_buy:
            reasons.append(Reason('buy-not-allowed', '매수 권한(LIVE_ALLOW_BUY)이 꺼져 있습니다.'))
        if intent.side == 'SELL' and not self.config.allow_sell:
            reasons.append(Reason('sell-not-allowed', '매도 권한(LIVE_ALLOW_SELL)이 꺼져 있습니다.'))
        if not self.broker.can_trade:
            reasons.append(Reason('no-broker', '주문을 보낼 수 있는 브로커 어댑터가 없습니다.'))
        if self.book['halt']['active']:
            reasons.append(Reason('halted', self.book['halt']['reason'] or '거래가 중단된 상태입니다.'))
        if self.unresolved():
            reasons.append(Reason('unresolved-orders', '결과를 확인하지 못한 주문이 있어 새 주문을 보내지 않습니다.'))
        suggested = 0
        if not problems:
            reasons.extend(Reason(v.code, v.message) for v in check_limits(intent, usage, self.config.limits))
            suggested = max_allowed_quantity(intent, usage, self.config.limits)
        return Evaluation(tuple(reasons), suggested)

    # ---- preview -> confirm -> execute --------------------------------------------------------------
    def _sign(self, preview_id, digest, nonce, expires_at):
        message = f'{preview_id}|{digest}|{nonce}|{expires_at}'.encode()
        return hmac.new(self.secret, message, hashlib.sha256).hexdigest()[:32]

    def _prune(self, now):
        previews = self.book['previews']
        for preview_id in [k for k, v in previews.items() if now > v['expires_at']+TOKEN_TTL*5]:
            del previews[preview_id]
        for preview_id in sorted(previews, key=lambda k: previews[k]['created_at'])[:-MAX_PREVIEWS]:
            del previews[preview_id]

    def preview(self, intent, usage):
        evaluation = self.evaluate(intent, usage)
        if evaluation.blocked:
            return Preview(False, evaluation, intent)
        now = int(self.clock())
        self._prune(now)
        preview_id, nonce, digest = secrets.token_hex(8), secrets.token_hex(8), intent.digest()
        expires_at = now+TOKEN_TTL
        cid = client_order_id(digest, nonce)
        self.book['previews'][preview_id] = {'intent': intent.to_dict(), 'digest': digest, 'nonce': nonce,
                                             'client_order_id': cid, 'expires_at': expires_at,
                                             'used': False, 'created_at': now}
        token = preview_id+'.'+self._sign(preview_id, digest, nonce, expires_at)
        return Preview(True, evaluation, intent, token, cid, expires_at)

    def begin_execute(self, token, usage):
        """Validate and consume the token, record the order as SUBMITTING. No broker call happens here, so a
        caller can persist this step first and call the broker outside any database transaction."""
        if not self.config.enabled or not self.broker.can_trade:
            raise LiveTradingLocked(self.config.reason)
        preview_id, _, signature = str(token).partition('.')
        record = self.book['previews'].get(preview_id)
        if record is None:
            raise InvalidConfirmation('확인 토큰을 찾을 수 없습니다.')
        expected = self._sign(preview_id, record['digest'], record['nonce'], record['expires_at'])
        if not hmac.compare_digest(expected.encode(), signature.encode()):
            raise InvalidConfirmation('확인 토큰이 올바르지 않습니다.')
        if record['used']:
            raise InvalidConfirmation('이미 사용한 확인 토큰입니다.')
        now = self.clock()
        if now > record['expires_at']:
            raise InvalidConfirmation('확인 토큰이 만료됐습니다. 미리보기를 다시 하세요.')
        intent = OrderIntent.from_dict(record['intent'])
        if intent.digest() != record['digest']:
            raise InvalidConfirmation('주문 내용이 미리보기와 다릅니다.')
        evaluation = self.evaluate(intent, usage)
        if evaluation.blocked:
            raise PolicyBlocked('; '.join(reason.message for reason in evaluation.reasons))
        record['used'] = True          # burn first: a crash after this point can never replay the token
        cid = record['client_order_id']
        self.book['orders'][cid] = {'client_order_id': cid, 'intent': record['intent'], 'state': st.SUBMITTING,
                                    'order_id': None, 'submitted_at': now, 'updated_at': now,
                                    'filled_quantity': 0, 'history': [[now, st.SUBMITTING, '확인 토큰 사용']]}
        return Submission(cid, intent)

    def complete_execute(self, cid, *, order_id=None, rejected='', uncertain=''):
        record = self.book['orders'][cid]
        if uncertain:
            self._move(record, st.UNKNOWN, uncertain)
        elif rejected:
            self._move(record, st.REJECTED, rejected)
        else:
            record['order_id'] = order_id
            self._move(record, st.ACCEPTED, '접수')
        self._sync_halt()
        return record

    def execute(self, token, usage):
        submission = self.begin_execute(token, usage)
        cid = submission.client_order_id
        try:
            order = self.broker.place_order(submission.intent, cid)
        except BrokerRejected as exc:
            self.complete_execute(cid, rejected=str(exc) or '거부됨')
            raise
        except BrokerUncertain as exc:
            self.complete_execute(cid, uncertain=str(exc) or '결과 불명확')
            raise
        except Exception as exc:
            # Anything unexpected may still have reached the broker, so it is never "not placed".
            self.complete_execute(cid, uncertain=f'{type(exc).__name__}: {exc}')
            raise BrokerUncertain('주문 결과를 확인하지 못했습니다.') from exc
        return self.complete_execute(cid, order_id=order.order_id)

    # ---- order state ---------------------------------------------------------------------------------
    def _move(self, record, new, note):
        if not st.can_transition(record['state'], new):
            raise ValueError(f'{record["state"]} -> {new} 전이는 허용되지 않습니다.')
        now = self.clock()
        record['state'], record['updated_at'] = new, now
        record['history'].append([now, new, note])

    def sync_order(self, cid, broker_order):
        """Adopt the broker's view of one of our orders. An unrecognised or illegal change is logged, not applied."""
        record = self.book['orders'][cid]
        record['order_id'] = broker_order.order_id
        # A fill can only grow: a stale or odd report must never make executed shares disappear.
        record['filled_quantity'] = max(record['filled_quantity'], int(broker_order.filled_quantity))
        new = st.map_broker_status(broker_order.status)
        if new == st.UNKNOWN or new == record['state']:
            if new == st.UNKNOWN:
                record['history'].append([self.clock(), record['state'], f'알 수 없는 브로커 상태 {broker_order.status!r}'])
            return record
        if st.can_transition(record['state'], new):
            self._move(record, new, f'브로커 상태 {broker_order.status}')
        else:
            record['history'].append([self.clock(), record['state'], f'허용되지 않는 전이 무시: {broker_order.status}'])
        self._sync_halt()
        return record

    def resolve_unknown(self, cid):
        """Look for evidence about an UNKNOWN order in the broker's order history."""
        record = self.book['orders'][cid]
        if record['state'] != st.UNKNOWN:
            return Resolution('not-unknown')
        intent, now = OrderIntent.from_dict(record['intent']), self.clock()
        claimed = {r['order_id'] for k, r in self.book['orders'].items() if k != cid and r.get('order_id')}
        found = [o for o in self.broker.find_orders(intent.symbol)
                 if orders_match(intent, record['submitted_at'], o, now) and o.order_id not in claimed]
        if len(found) == 1:
            self.sync_order(cid, found[0])
            record['resolution'] = 'adopted'
            return Resolution('adopted', found[0].order_id)
        if len(found) > 1:
            record['resolution'] = 'ambiguous'
            return Resolution('ambiguous', f'일치하는 주문이 {len(found)}건입니다. 직접 확인이 필요합니다.')
        if now-record['submitted_at'] <= RETRY_WINDOW:
            record['resolution'] = 'retry-allowed'
            return Resolution('retry-allowed', '이력에 없고 멱등키 유효 시간 안이라 같은 키로 재전송할 수 있습니다.')
        record['resolution'] = 'manual'
        return Resolution('manual', '유효 시간이 지나 자동 재전송할 수 없습니다. 직접 확인 후 처리하세요.')

    def retry_same_id(self, cid):
        """Resend an UNKNOWN order with the SAME client id: inside the broker's window this replays the earlier
        result instead of creating a second order."""
        if not self.config.enabled or not self.broker.can_trade:
            raise LiveTradingLocked(self.config.reason)
        record = self.book['orders'][cid]
        now = self.clock()
        if record['state'] != st.UNKNOWN or record.get('resolution') != 'retry-allowed' \
                or now-record['submitted_at'] > RETRY_WINDOW:
            raise PolicyBlocked('이 주문은 같은 키로 재전송할 수 없습니다.')
        try:
            order = self.broker.place_order(OrderIntent.from_dict(record['intent']), cid)
        except BrokerRejected as exc:
            self._move(record, st.REJECTED, str(exc) or '거부됨')
            self._sync_halt()
            raise
        except Exception as exc:
            record['history'].append([now, st.UNKNOWN, f'재전송 결과 불명확: {type(exc).__name__}'])
            raise BrokerUncertain('재전송 결과를 확인하지 못했습니다.') from exc
        record['order_id'] = order.order_id
        self._move(record, st.ACCEPTED, '같은 키로 재전송 후 접수')
        self._sync_halt()
        return record

    def mark_not_placed(self, cid, operator, note):
        """An operator's explicit decision that an UNKNOWN order never reached the broker."""
        if not str(operator).strip() or not str(note).strip():
            raise PolicyBlocked('처리자와 사유가 필요합니다.')
        self._move(self.book['orders'][cid], st.NOT_PLACED, f'operator={operator}: {note}')
        self._sync_halt()

    # ---- halt ------------------------------------------------------------------------------------------
    def _sync_halt(self):
        halt, unresolved = self.book['halt'], self.unresolved()
        if unresolved and not halt['active']:
            halt.update(active=True, manual=False, reason=f'결과를 확인하지 못한 주문 {len(unresolved)}건', since=self.clock())
        elif not unresolved and halt['active'] and not halt['manual']:
            halt.update(active=False, reason='', since=0)

    def halt(self, reason):
        self.book['halt'].update(active=True, manual=True, reason=str(reason), since=self.clock())

    def release_halt(self, operator):
        if not str(operator).strip():
            raise PolicyBlocked('처리자가 필요합니다.')
        if self.unresolved():
            raise PolicyBlocked('결과를 확인하지 못한 주문이 남아 있어 중단을 해제할 수 없습니다.')
        self.book['halt'].update(active=False, manual=False, reason='', since=0)
