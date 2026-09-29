"""The order gate exercised against a scripted fake broker. Nothing here touches the network."""
import json
import re
from decimal import Decimal

import pytest

from app.live import lifecycle as st
from app.live.broker import Broker, BrokerOrder, DisabledBroker
from app.live.errors import BrokerRejected, BrokerUncertain, InvalidConfirmation, LiveTradingLocked, PolicyBlocked
from app.live.gate import RETRY_WINDOW, TOKEN_TTL, OrderGate, new_book, usage_from_book
from app.live.intent import OrderIntent, client_order_id
from app.live.limits import LimitUsage, LiveLimits
from app.live.lock import LiveConfig

NOW = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class FakeBroker(Broker):
    name = 'fake'
    can_trade = True

    def __init__(self, clock):
        self.clock, self.mode, self.placed, self.orders = clock, 'ok', [], []

    def _order(self, intent, status='PENDING'):
        return BrokerOrder(order_id=f'order-{len(self.orders)+1}', symbol=intent.symbol, side=intent.side,
                           quantity=Decimal(intent.quantity),
                           limit_price=intent.limit_price if intent.order_type == 'LIMIT' else None,
                           status=status, ordered_at=self.clock())

    def place_order(self, intent, client_order_id):
        self.placed.append((intent, client_order_id))
        if self.mode == 'reject':
            raise BrokerRejected('가격이 호가 단위에 맞지 않습니다.')
        if self.mode == 'timeout':
            raise BrokerUncertain('timeout')
        if self.mode == 'crash':
            raise RuntimeError('boom')
        order = self._order(intent)
        self.orders.append(order)
        if self.mode == 'timeout-but-placed':
            raise BrokerUncertain('response lost')
        return order

    def find_orders(self, symbol):
        return [o for o in self.orders if o.symbol == symbol]

    def normalize_price(self, symbol, price, side):
        return price

    cancel_order = snapshot = list_protective = place_protective = cancel_protective = lambda self, *a, **k: None


def live_config(**overrides):
    values = dict(requested=True, enabled=True, allow_buy=True, allow_sell=True, shadow=True,
                  limits=LiveLimits.default(), reason='on')
    values.update(overrides)
    return LiveConfig(**values)


def intent(**overrides):
    values = dict(symbol='AAPL', market='US', currency='USD', side='BUY', quantity=1, order_type='LIMIT',
                  limit_price=Decimal('100'), reference_price=Decimal('100'))
    values.update(overrides)
    return OrderIntent(**values)


@pytest.fixture
def rig():
    clock = Clock()
    broker = FakeBroker(clock)
    gate = OrderGate(new_book(), broker, live_config(), b'test-secret', clock)
    return gate, broker, clock


def usage(**overrides):
    return LimitUsage(**overrides)


# ---- locked build ------------------------------------------------------------------------------------
def test_locked_config_blocks_everything_and_issues_no_token():
    gate = OrderGate(new_book(), DisabledBroker(), LiveConfig(), b'k')
    preview = gate.preview(intent(), usage())
    assert not preview.ok and preview.token is None and preview.client_order_id is None
    assert {'locked', 'no-broker', 'buy-not-allowed'} <= set(preview.evaluation.codes())
    with pytest.raises(LiveTradingLocked):
        gate.execute('anything.at-all', usage())
    assert gate.book['orders'] == {} and gate.book['previews'] == {}


# ---- preview -> confirm -> execute ------------------------------------------------------------------------
def test_happy_path_sends_one_order_with_an_idempotency_key(rig):
    gate, broker, _ = rig
    preview = gate.preview(intent(), usage())
    assert preview.ok and preview.token and preview.expires_at == int(NOW)+TOKEN_TTL
    record = gate.execute(preview.token, usage())
    assert record['state'] == st.ACCEPTED and record['order_id'] == 'order-1'
    assert len(broker.placed) == 1 and broker.placed[0][1] == preview.client_order_id
    assert len(preview.client_order_id) == 36 and re.fullmatch(r'[a-z0-9]+', preview.client_order_id)


def test_token_works_once(rig):
    gate, broker, _ = rig
    preview = gate.preview(intent(), usage())
    gate.execute(preview.token, usage())
    with pytest.raises(InvalidConfirmation, match='이미 사용'):
        gate.execute(preview.token, usage())
    assert len(broker.placed) == 1


def test_tampered_expired_and_foreign_tokens_are_refused(rig):
    gate, broker, clock = rig
    preview = gate.preview(intent(), usage())
    with pytest.raises(InvalidConfirmation):
        gate.execute(preview.token[:-1]+('0' if preview.token[-1] != '0' else '1'), usage())
    with pytest.raises(InvalidConfirmation):
        gate.execute('nope', usage())
    other = OrderGate(new_book(), broker, live_config(), b'another-secret', clock)
    foreign = other.preview(intent(), usage())
    with pytest.raises(InvalidConfirmation):
        gate.execute(foreign.token, usage())
    clock.now += TOKEN_TTL+1
    with pytest.raises(InvalidConfirmation, match='만료'):
        gate.execute(preview.token, usage())
    assert broker.placed == []


def test_an_order_edited_after_preview_is_refused(rig):
    gate, broker, _ = rig
    preview = gate.preview(intent(), usage())
    preview_id = preview.token.split('.')[0]
    gate.book['previews'][preview_id]['intent']['quantity'] = 9
    with pytest.raises(InvalidConfirmation, match='다릅니다'):
        gate.execute(preview.token, usage())
    assert broker.placed == []


def test_limits_are_checked_again_at_execute_and_the_token_is_kept(rig):
    gate, broker, _ = rig
    preview = gate.preview(intent(), usage())
    with pytest.raises(PolicyBlocked, match='하루 주문 횟수'):
        gate.execute(preview.token, usage(orders_today=10))
    assert not gate.book['previews'][preview.token.split('.')[0]]['used'] and broker.placed == []
    assert gate.execute(preview.token, usage()).get('state') == st.ACCEPTED


def test_client_order_id_is_deterministic_per_preview_and_differs_across_previews(rig):
    gate, _, _ = rig
    a, b = gate.preview(intent(), usage()), gate.preview(intent(), usage())
    assert a.client_order_id != b.client_order_id
    assert client_order_id('d', 'n') == client_order_id('d', 'n') and len(client_order_id('d', 'n')) == 36


# ---- limits ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize('order,used,code', [
    (dict(symbol='TSLA'), {}, 'symbol-not-allowed'),
    (dict(symbol='TQQQ'), {}, 'symbol-not-allowed'),
    (dict(quantity=2), {}, 'max-order'),
    ({}, dict(orders_today=10), 'max-daily-orders'),
    ({}, dict(notional_today=Decimal(250)), 'max-daily-notional'),
    ({}, dict(open_orders=3), 'max-open-orders'),
    ({}, dict(position_notional=Decimal(150)), 'max-position'),
    ({}, dict(loss_today=Decimal(30)), 'daily-loss-halt'),
    (dict(limit_price=Decimal('102')), {}, 'price-band'),
])
def test_buy_limits_each_block(rig, order, used, code):
    gate, _, _ = rig
    evaluation = gate.evaluate(intent(**order), usage(**used))
    assert code in evaluation.codes() and not gate.preview(intent(**order), usage(**used)).ok


def test_a_buy_inside_every_limit_has_no_reasons(rig):
    gate, _, _ = rig
    assert gate.evaluate(intent(), usage()).codes() == []


def test_sells_are_never_trapped_by_size_or_count_limits(rig):
    gate, _, _ = rig
    huge = intent(side='SELL', symbol='TQQQ', quantity=500, limit_price=Decimal('100'))
    used = usage(orders_today=99, notional_today=Decimal(10**6), open_orders=9, loss_today=Decimal(10**6),
                 held_quantity=500)
    assert gate.evaluate(huge, used).codes() == []


def test_selling_more_than_held_is_refused_and_shorting_is_impossible(rig):
    gate, _, _ = rig
    assert gate.evaluate(intent(side='SELL', quantity=5), usage(held_quantity=3)).codes() == ['exceeds-position']
    assert gate.evaluate(intent(side='SELL', quantity=5), usage(held_quantity=0)).codes() == ['exceeds-position']


def test_suggested_quantity_fits_the_limits(rig):
    gate, _, _ = rig
    evaluation = gate.evaluate(intent(quantity=5), usage())
    assert 'max-order' in evaluation.codes() and evaluation.suggested_quantity == 1
    assert gate.evaluate(intent(quantity=5), usage(loss_today=Decimal(30))).suggested_quantity == 0


def test_invalid_orders_are_reported_without_crashing(rig):
    gate, _, _ = rig
    bad = intent(quantity=0, limit_price=Decimal(0))
    evaluation = gate.evaluate(bad, usage())
    assert 'invalid-intent' in evaluation.codes() and evaluation.suggested_quantity == 0


def test_permissions_for_buy_and_sell_are_separate():
    clock = Clock()
    gate = OrderGate(new_book(), FakeBroker(clock), live_config(allow_sell=False), b'k', clock)
    assert gate.evaluate(intent(), usage()).codes() == []
    assert gate.evaluate(intent(side='SELL'), usage(held_quantity=1)).codes() == ['sell-not-allowed']


# ---- outcomes ----------------------------------------------------------------------------------------------
def test_a_definitive_rejection_is_recorded_and_does_not_halt(rig):
    gate, broker, _ = rig
    broker.mode = 'reject'
    preview = gate.preview(intent(), usage())
    with pytest.raises(BrokerRejected):
        gate.execute(preview.token, usage())
    record = gate.book['orders'][preview.client_order_id]
    assert record['state'] == st.REJECTED and not gate.book['halt']['active']
    assert gate.evaluate(intent(), usage()).codes() == []


@pytest.mark.parametrize('mode', ['timeout', 'crash', 'timeout-but-placed'])
def test_an_unclear_outcome_becomes_unknown_and_halts_new_orders(rig, mode):
    gate, broker, _ = rig
    broker.mode = mode
    preview = gate.preview(intent(), usage())
    with pytest.raises(BrokerUncertain):
        gate.execute(preview.token, usage())
    assert gate.book['orders'][preview.client_order_id]['state'] == st.UNKNOWN
    assert gate.book['halt']['active'] and gate.unresolved() == [preview.client_order_id]
    assert {'halted', 'unresolved-orders'} <= set(gate.evaluate(intent(), usage()).codes())
    assert len(broker.placed) == 1              # never resent automatically


def unknown_order(rig, mode='timeout'):
    gate, broker, clock = rig
    broker.mode = mode
    preview = gate.preview(intent(), usage())
    with pytest.raises(BrokerUncertain):
        gate.execute(preview.token, usage())
    broker.mode = 'ok'
    return preview.client_order_id


def test_resolving_an_unknown_order_adopts_the_single_matching_broker_order(rig):
    gate, broker, _ = rig
    cid = unknown_order(rig, 'timeout-but-placed')
    result = gate.resolve_unknown(cid)
    assert result.action == 'adopted' and gate.book['orders'][cid]['state'] == st.ACCEPTED
    assert not gate.book['halt']['active'] and len(broker.placed) == 1


def test_two_matching_broker_orders_stay_ambiguous_and_keep_the_halt(rig):
    gate, broker, _ = rig
    cid = unknown_order(rig, 'timeout-but-placed')
    broker.orders.append(broker._order(intent()))
    assert gate.resolve_unknown(cid).action == 'ambiguous'
    assert gate.book['orders'][cid]['state'] == st.UNKNOWN and gate.book['halt']['active']


def test_not_found_inside_the_window_allows_a_resend_with_the_same_key(rig):
    gate, broker, clock = rig
    cid = unknown_order(rig, 'timeout')
    assert gate.resolve_unknown(cid).action == 'retry-allowed'
    record = gate.retry_same_id(cid)
    assert record['state'] == st.ACCEPTED and [c for _, c in broker.placed] == [cid, cid]
    assert not gate.book['halt']['active']


def test_after_the_window_only_a_person_can_resolve(rig):
    gate, broker, clock = rig
    cid = unknown_order(rig, 'timeout')
    clock.now += RETRY_WINDOW+1
    assert gate.resolve_unknown(cid).action == 'manual'
    with pytest.raises(PolicyBlocked):
        gate.retry_same_id(cid)
    with pytest.raises(PolicyBlocked):
        gate.mark_not_placed(cid, '', '')
    gate.mark_not_placed(cid, 'kim', '증권사 앱에서 주문 없음 확인')
    assert gate.book['orders'][cid]['state'] == st.NOT_PLACED and not gate.book['halt']['active']
    assert 'kim' in gate.book['orders'][cid]['history'][-1][2]


def test_a_retry_that_is_rejected_ends_as_rejected(rig):
    gate, broker, _ = rig
    cid = unknown_order(rig, 'timeout')
    gate.resolve_unknown(cid)
    broker.mode = 'reject'
    with pytest.raises(BrokerRejected):
        gate.retry_same_id(cid)
    assert gate.book['orders'][cid]['state'] == st.REJECTED and not gate.book['halt']['active']


def test_a_manual_halt_stays_until_a_person_releases_it(rig):
    gate, _, _ = rig
    gate.halt('점검')
    assert 'halted' in gate.evaluate(intent(), usage()).codes()
    with pytest.raises(PolicyBlocked):
        gate.release_halt('')
    gate.release_halt('kim')
    assert gate.evaluate(intent(), usage()).codes() == []


def test_broker_status_updates_follow_the_lifecycle(rig):
    gate, broker, _ = rig
    preview = gate.preview(intent(quantity=1), usage())
    gate.execute(preview.token, usage())
    cid = preview.client_order_id
    order = broker.orders[0]

    def update(status, filled=0):
        gate.sync_order(cid, BrokerOrder(order.order_id, order.symbol, order.side, order.quantity,
                                         Decimal(filled), order.limit_price, status, order.ordered_at))
        return gate.book['orders'][cid]
    assert update('PARTIAL_FILLED', 1)['state'] == st.PARTIALLY_FILLED
    assert update('FILLED', 1)['state'] == st.FILLED
    stale = update('PENDING', 0)                                        # illegal move is logged, not applied
    assert stale['state'] == st.FILLED and stale['filled_quantity'] == 1  # and a fill never shrinks
    assert 'PENDING' in gate.book['orders'][cid]['history'][-1][2]
    assert update('WEIRD_NEW_STATUS')['state'] == st.FILLED
    assert 'WEIRD_NEW_STATUS' in gate.book['orders'][cid]['history'][-1][2]


# ---- accounting and persistence -------------------------------------------------------------------------------
def test_usage_from_book_counts_todays_exposure_per_currency(rig):
    gate, broker, clock = rig
    for _ in range(2):
        gate.execute(gate.preview(intent(), usage()).token, usage())
    broker.mode = 'reject'
    with pytest.raises(BrokerRejected):
        gate.execute(gate.preview(intent(), usage()).token, usage())
    used = usage_from_book(gate.book, intent(), clock(), held_quantity=2)
    assert (used.orders_today, used.notional_today, used.open_orders, used.held_quantity) == (2, Decimal(200), 2, 2)
    krw = intent(symbol='005930', market='KR', currency='KRW', limit_price=Decimal(70000), reference_price=Decimal(70000))
    assert usage_from_book(gate.book, krw, clock()).orders_today == 0
    clock.now += 3*86400
    assert usage_from_book(gate.book, intent(), clock()).orders_today == 0


def test_the_book_survives_a_json_round_trip_so_it_can_be_persisted(rig):
    gate, broker, _ = rig
    cid = unknown_order(rig, 'timeout')
    restored = json.loads(json.dumps(gate.book))
    again = OrderGate(restored, broker, live_config(), b'test-secret', Clock())
    assert again.unresolved() == [cid] and again.book['halt']['active']
