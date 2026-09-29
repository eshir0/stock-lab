"""Reconciliation, protective-order planning, lifecycle mapping, limits config and the shadow recorder."""
from decimal import Decimal

import pytest

from app.live import lifecycle as st
from app.live.broker import BrokerOrder, BrokerSnapshot, orders_match
from app.live.intent import OrderIntent
from app.live.limits import LiveLimits
from app.live.lock import LiveConfig
from app.live.protective import diff_protective, plan_protective_orders
from app.live.reconcile import reconcile
from app.live.shadow import MAX_SHADOW, record_shadow, shadow_summary

NOW = 1_800_000_000.0


def intent(**overrides):
    values = dict(symbol='AAPL', market='US', currency='USD', side='BUY', quantity=1, order_type='LIMIT',
                  limit_price=Decimal('100'), reference_price=Decimal('100'))
    values.update(overrides)
    return OrderIntent(**values)


# ---- lifecycle / intent ------------------------------------------------------------------------------------
@pytest.mark.parametrize('raw,expected', [
    ('PENDING', st.ACCEPTED), ('PENDING_REPLACE', st.ACCEPTED), ('PARTIAL_FILLED', st.PARTIALLY_FILLED),
    ('FILLED', st.FILLED), ('PENDING_CANCEL', st.CANCEL_PENDING), ('CANCELED', st.CANCELED), ('REJECTED', st.REJECTED),
    ('CANCEL_REJECTED', st.UNKNOWN), ('REPLACE_REJECTED', st.UNKNOWN), ('REPLACED', st.UNKNOWN), ('NEW_STATUS', st.UNKNOWN)])
def test_broker_statuses_map_and_unrecognised_ones_are_never_guessed(raw, expected):
    assert st.map_broker_status(raw) == expected


def test_lifecycle_transitions():
    assert st.can_transition(st.SUBMITTING, st.UNKNOWN) and st.can_transition(st.UNKNOWN, st.NOT_PLACED)
    assert not st.can_transition(st.FILLED, st.ACCEPTED) and not st.can_transition(st.REJECTED, st.ACCEPTED)
    assert not st.can_transition(st.SUBMITTING, st.FILLED)          # must be acknowledged first
    assert st.UNKNOWN in st.LIVE_EXPOSURE and st.FILLED in st.TERMINAL


def test_intent_validation_and_stable_digest():
    assert intent().problems() == []
    assert intent().digest() == intent().digest() != intent(quantity=2).digest()
    for bad in (dict(quantity=0), dict(quantity=1.5), dict(side='HOLD'), dict(order_type='STOP'),
                dict(limit_price=Decimal(0)), dict(reference_price=Decimal(0)), dict(currency='KRW'),
                dict(symbol='../orders')):
        assert intent(**bad).problems(), bad
    assert OrderIntent.from_dict(intent().to_dict()) == intent()
    assert intent(order_type='MARKET', limit_price=Decimal(0)).problems() == []


# ---- limits configuration -------------------------------------------------------------------------------------
def test_limits_defaults_are_conservative_and_exclude_leveraged_etfs():
    limits = LiveLimits.default()
    assert {'005930', 'AAPL'} <= limits.symbols and not {'TQQQ', 'SQQQ', '122630'} & limits.symbols
    assert limits.KRW.max_order == 100000 and limits.USD.max_order == 100


def test_limits_can_be_set_from_the_environment():
    limits = LiveLimits.from_env({'LIVE_MAX_ORDER_KRW': '50000', 'LIVE_ALLOWED_SYMBOLS': '005930, aapl',
                                  'LIVE_MAX_DAILY_ORDERS': '4', 'LIVE_PRICE_BAND_PCT': '0.5'})
    assert limits.KRW.max_order == 50000 and limits.symbols == frozenset({'005930', 'AAPL'})
    assert limits.KRW.max_daily_orders == limits.USD.max_daily_orders == 4 and limits.price_band_pct == Decimal('0.5')
    assert limits.USD.max_order == 100                                # untouched values keep their defaults


@pytest.mark.parametrize('env', [{'LIVE_MAX_ORDER_KRW': 'lots'}, {'LIVE_MAX_ORDER_USD': '-5'},
                                 {'LIVE_MAX_DAILY_ORDERS': '2.5'}, {'LIVE_MAX_POSITION_KRW': 'nan'}])
def test_invalid_limit_values_fail_loudly(env):
    with pytest.raises(ValueError):
        LiveLimits.from_env(env)


def test_an_empty_allow_list_allows_nothing():
    assert LiveLimits.from_env({'LIVE_ALLOWED_SYMBOLS': ''}).symbols == frozenset()


def test_public_config_is_json_friendly():
    import json
    payload = LiveConfig.from_env({}).public()
    assert json.loads(json.dumps(payload))['locked'] is True and payload['limits']['KRW']['max_order'] == '100000'


# ---- reconciliation -------------------------------------------------------------------------------------------
def gate_record(state=st.ACCEPTED, **overrides):
    order = intent(**overrides)
    return {'client_order_id': 'c1', 'intent': order.to_dict(), 'state': state, 'order_id': 'o1',
            'submitted_at': NOW, 'filled_quantity': 0, 'history': []}


def broker_order(status='PENDING', order_id='o1', **overrides):
    values = dict(order_id=order_id, symbol='AAPL', side='BUY', quantity=Decimal(1), limit_price=Decimal('100'),
                  status=status, ordered_at=NOW)
    values.update(overrides)
    return BrokerOrder(**values)


def snapshot(**overrides):
    values = dict(taken_at=NOW, cash={'KRW': Decimal(1000000), 'USD': Decimal(1000)}, positions={'AAPL': Decimal(2)})
    values.update(overrides)
    return BrokerSnapshot(**values)


LEDGER = ({'AAPL': 2}, {'KRW': 1000000, 'USD': 1000})


def test_matching_ledger_and_broker_reconcile_cleanly():
    report = reconcile(*LEDGER, [], snapshot(), NOW)
    assert report.ok and not report.halt


def test_a_wrong_position_is_critical_and_halts():
    report = reconcile({'AAPL': 2}, LEDGER[1], [], snapshot(positions={'AAPL': Decimal(3), 'MSFT': Decimal(1)}), NOW)
    assert report.halt and {d.subject for d in report.diffs if d.kind == 'position-mismatch'} == {'AAPL', 'MSFT'}


@pytest.mark.parametrize('usd,severity', [(1000.5, None), (1003, None), (1020, 'warning'), (1100, 'critical')])
def test_cash_drift_is_graded(usd, severity):
    report = reconcile(LEDGER[0], {'KRW': 1000000, 'USD': 1000}, [], snapshot(cash={'KRW': Decimal(1000000), 'USD': Decimal(str(usd))}), NOW)
    assert (report.diffs[0].severity if report.diffs else None) == severity
    assert report.halt == (severity == 'critical')


def test_orders_are_matched_and_drift_or_absence_is_reported():
    ok = reconcile(*LEDGER, [gate_record()], snapshot(open_orders=(broker_order(),)), NOW)
    assert ok.ok
    drift = reconcile(*LEDGER, [gate_record()], snapshot(closed_orders=(broker_order('FILLED'),)), NOW)
    assert [d.kind for d in drift.diffs] == ['order-state-drift'] and not drift.halt
    missing = reconcile(*LEDGER, [gate_record()], snapshot(), NOW)
    assert [d.kind for d in missing.diffs] == ['order-missing'] and missing.halt
    in_flight = reconcile(*LEDGER, [gate_record(st.UNKNOWN)], snapshot(), NOW)
    assert in_flight.diffs[0].severity == 'warning' and not in_flight.halt


def test_orders_the_app_did_not_send_are_flagged_but_not_fatal():
    report = reconcile(*LEDGER, [], snapshot(open_orders=(broker_order(order_id='ext'),)), NOW)
    assert [d.kind for d in report.diffs] == ['external-order'] and not report.halt


def test_order_matching_uses_symbol_side_quantity_price_and_time():
    base = intent()
    assert orders_match(base, NOW, broker_order(), NOW)
    for changed in (broker_order(symbol='MSFT'), broker_order(side='SELL'), broker_order(quantity=Decimal(2)),
                    broker_order(limit_price=Decimal('101')), broker_order(ordered_at=NOW-600)):
        assert not orders_match(base, NOW, changed, NOW)
    assert orders_match(intent(order_type='MARKET', limit_price=Decimal(0)), NOW, broker_order(limit_price=None), NOW)


# ---- protective orders ---------------------------------------------------------------------------------------------
def plan(**overrides):
    values = dict(symbol='AAPL', market='US', currency='USD', quantity=3, stop_price='98', take_profit_price='104',
                  last_price='100', expire_date='2026-09-29')
    values.update(overrides)
    return plan_protective_orders(**values)


def test_a_valid_position_gets_one_oco_plan_with_the_stop_limit_below_the_trigger():
    result = plan()
    assert result.problems == () and result.plan.quantity == 3
    assert result.plan.stop_trigger == 98 and result.plan.stop_limit == Decimal('98')*Decimal('0.995')
    assert result.plan.take_profit_trigger == result.plan.take_profit_limit == 104
    assert any('지정가' in note for note in result.plan.notes)


@pytest.mark.parametrize('overrides', [dict(last_price='98'), dict(last_price='97'), dict(last_price='104'),
                                       dict(last_price='105'), dict(quantity=0), dict(stop_price='0')])
def test_no_plan_when_the_price_is_already_through_a_level(overrides):
    result = plan(**overrides)
    assert result.plan is None and result.problems


def existing(**overrides):
    values = dict(id='p1', symbol='AAPL', status='OPEN', quantity=3, expire_date='2026-09-29',
                  stop_trigger='98', take_profit_trigger='104')
    values.update(overrides)
    return values


def test_desired_state_sync_creates_replaces_and_cancels():
    wanted = plan().plan
    assert [a.kind for a in diff_protective([wanted], [])] == ['create']
    assert diff_protective([wanted], [existing()]) == []
    assert diff_protective([wanted], [existing(stop_trigger='98.05')]) == []           # tick rounding tolerated
    changed = diff_protective([wanted], [existing(quantity=2)])
    assert [(a.kind, a.protective_id) for a in changed] == [('replace', 'p1')]
    orphan = diff_protective([wanted], [existing(), existing(id='p2', symbol='MSFT'), existing(id='p3')])
    assert sorted((a.kind, a.protective_id) for a in orphan) == [('cancel', 'p2'), ('cancel', 'p3')]
    assert diff_protective([], [existing(status='CLOSED')]) == []


# ---- shadow recorder -------------------------------------------------------------------------------------------------
CONFIG = LiveConfig.from_env({})


def proposal(**overrides):
    values = dict(id='pr1', symbol='AAPL', side='BUY', quantity=1, reference_price=100.0, status='filled', run_id='run1')
    values.update(overrides)
    return values


def instrument(symbol='AAPL', market='US', currency='USD'):
    return {'symbol': symbol, 'market': market, 'currency': currency}


QUOTE = {'bid': 99.9, 'ask': 100.0}
SIZING = {'stop_price': 98.0, 'take_profit_price': 104.0, 'expires_at': NOW+3600}


def shadow(state, **overrides):
    values = dict(proposal=proposal(), quote=QUOTE, instrument=instrument(), config=CONFIG, source='ai', now=NOW, sizing=SIZING)
    values.update(overrides)
    return record_shadow(state, **values)


def test_a_buy_inside_the_limits_would_be_submitted_with_a_protective_plan():
    state = {}
    record = shadow(state)
    assert record['would_submit'] and record['blocked_by'] == [] and record['order_type'] == 'LIMIT'
    assert record['limit_price'] == '100.0' and record['notional'] == '100.0'
    assert record['protective']['stop_trigger'] == '98.0' and record['protective']['quantity'] == 1
    assert state['shadow_orders'] == [record]


def test_shadow_orders_count_against_the_hypothetical_daily_limits():
    state, cheap = {}, dict(quote={'bid': 19.9, 'ask': 20.0}, sizing=None)
    orders = [shadow(state, proposal=proposal(id=f'p{i}', reference_price=20.0), **cheap) for i in range(12)]
    assert [r['would_submit'] for r in orders] == [True]*10+[False]*2
    assert orders[-1]['blocked_by'] == ['max-daily-orders']
    state = {}
    by_amount = [shadow(state, proposal=proposal(id=f'n{i}')) for i in range(5)]      # 100 USD each, 300 USD a day
    assert [r['would_submit'] for r in by_amount] == [True]*3+[False]*2
    assert by_amount[3]['blocked_by'] == ['max-daily-notional']


def test_an_oversized_or_disallowed_order_is_blocked_with_a_suggested_size():
    state = {}
    big = shadow(state, proposal=proposal(quantity=5))
    assert not big['would_submit'] and 'max-order' in big['blocked_by'] and big['suggested_quantity'] == 1
    leveraged = shadow(state, proposal=proposal(symbol='TQQQ'), instrument=instrument('TQQQ'))
    assert 'symbol-not-allowed' in leveraged['blocked_by']


def test_exits_are_never_blocked_by_size_limits_but_cannot_oversell():
    state = {'positions': {'AAPL': {'quantity': 50}}}
    exit_order = shadow(state, proposal=proposal(side='SELL', quantity=50), sizing=None, source='exit')
    assert exit_order['would_submit'] and exit_order['protective'] is None
    oversell = shadow({'positions': {'AAPL': {'quantity': 3}}}, proposal=proposal(side='SELL', quantity=5), sizing=None)
    assert oversell['blocked_by'] == ['exceeds-position']


def test_the_daily_loss_limit_blocks_new_buys_in_the_shadow_account():
    state = {'risk_days': {'USD': {'baseline': 1000.0, 'last_equity': 960.0}}}
    assert shadow(state)['blocked_by'] == ['daily-loss-halt']


def test_shadow_records_are_capped_and_summarised():
    state = {}
    for i in range(MAX_SHADOW+20):
        shadow(state, proposal=proposal(id=f'p{i}', quantity=5), now=NOW+i*86400)
    assert len(state['shadow_orders']) == MAX_SHADOW
    summary = shadow_summary(state)
    assert summary['total'] == MAX_SHADOW and summary['blocked'] == MAX_SHADOW and summary['blocked_by']['max-order'] == MAX_SHADOW
    assert len(summary['recent']) == 10 and summary['by_side'] == {'BUY': MAX_SHADOW, 'SELL': 0}


def test_a_shadow_record_is_json_friendly():
    import json
    state = {}
    shadow(state)
    assert json.loads(json.dumps(state))['shadow_orders'][0]['symbol'] == 'AAPL'
