"""Isolated SQLite/demo checks: no production account or external API is used."""
import copy
import time

import pytest

from app.config import Config
from app.engine import Engine
from app.providers import DemoProvider, ProviderError
from app.store import Store


class FixedDeskProvider(DemoProvider):
    def __init__(self):
        self.price = 70000
        self.fail_refresh = False

    def quote(self, symbol):
        quote = super().quote(symbol)
        if symbol == '005930':
            quote.update(last=self.price, bid=self.price, ask=self.price)
        return quote

    def quotes(self):
        if self.fail_refresh:
            raise ProviderError('An unrelated universe quote is unavailable.')
        return super().quotes()


@pytest.fixture
def desk(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'desk.db'), mode='demo',
                    password='test-password-123456', session_secret='test-secret-123456789012345678901234',
                    toss_id='', toss_secret='', gemini_key='',
                    fee_kr=15, fee_us=15, sell_tax_kr=0, slippage_bps=5)
    store = Store(config.database_url, config.mode)
    engine = Engine(config, store, FixedDeskProvider())
    engine.boot()
    engine.new_experiment(1000000, 1000, 'Isolated intraday test', strategy_mode='intraday')
    engine.refresh()
    yield engine
    store.release()


def pending_stop_exit(engine):
    """Create an ordinary manual exit through the real fill/monitor path."""
    engine.start()
    quote = engine.provider.quote('005930')
    with engine.store.edit() as state:
        engine.fill(state, '005930', 'BUY', 2, quote, 'isolated-entry')
        engine.apply_desk_plan(state, '005930', {'side': 'BUY', 'summary': 'Test position'},
                               {'stop_price': 68600, 'take_profit_price': 72800,
                                'expires_at': time.time()+3600, 'quantity': 2})
    engine.provider.price = 68000
    engine.process_desk_exits()
    return engine.store.read()['proposals'][-1]['id']


@pytest.mark.parametrize('mode', ['manual', 'auto'])
def test_expired_exit_replaced_when_universe_refresh_fails(desk, mode):
    previous_id = pending_stop_exit(desk)
    with desk.store.edit() as state:
        state['proposals'][-1]['expires'] = time.time()-10
        # Also cover recovery of an expired pending record in automatic mode.
        state['execution_mode'] = mode
    desk.provider.fail_refresh = True
    desk.refresh()
    assert desk.store.read()['last_error']

    desk.process_desk_exits()
    state = desk.store.read()
    previous, replacement = state['proposals']
    assert previous['id'] == previous_id and previous['status'] == 'expired'
    assert replacement['id'] != previous_id
    assert replacement['expires'] > time.time()
    assert replacement['quantity'] == 2
    assert replacement['status'] == ('pending' if mode == 'manual' else 'filled')
    desk.process_desk_exits()
    assert len(desk.store.read()['proposals']) == 2

    if mode == 'manual':
        assert len(state['trades']) == 1 and state['positions']
        desk.approve(replacement['id'])
    state = desk.store.read()
    assert not state['positions']
    assert len(state['trades']) == 2
    assert state['trades'][-1]['side'] == 'SELL'
    assert state['trades'][-1]['exit_reason'] == '손절 조건'


def test_valid_pending_exit_is_not_duplicated_after_refresh_failure(desk):
    proposal_id = pending_stop_exit(desk)
    before = copy.deepcopy(desk.store.read()['proposals'])
    desk.provider.fail_refresh = True
    desk.refresh()
    desk.process_desk_exits()
    desk.process_desk_exits()
    state = desk.store.read()
    assert state['proposals'] == before
    assert state['proposals'][0]['id'] == proposal_id
    assert len(state['trades']) == 1 and state['positions']


@pytest.mark.parametrize('mode', ['manual', 'auto'])
def test_stop_keeps_intraday_position_and_cancels_exit_monitor(desk, mode):
    pending_stop_exit(desk)
    with desk.store.edit() as state:
        state['proposals'][-1]['expires'] = time.time()-10
        state['execution_mode'] = mode
    desk.stop()
    before = desk.store.read()
    desk.process_desk_exits()
    after = desk.store.read()
    assert after == before
    assert after['positions'] and len(after['trades']) == 1
    assert after['proposals'][0]['status'] == 'invalidated'


@pytest.mark.parametrize('mode', ['manual', 'auto'])
def test_demo_intraday_research_fill_exit_and_ledger(desk, mode):
    desk.set_execution(mode)
    desk.start()
    desk.cycle()
    state = desk.store.read()
    run = state['runs'][-1]
    assert run['status'] == 'completed'
    assert [report['role'] for report in run['reports']] == [
        'selector', 'planner', 'fundamental', 'technical', 'news', 'critic', 'director']
    assert run['selected_by'] == 'ai' and run['reports'][0]['symbol'] == run['symbol'] == '005930'
    entry = state['proposals'][-1]
    assert entry['side'] == 'BUY' and type(entry['quantity']) is int and entry['quantity'] > 0
    assert entry['sizing']['estimated_stop_risk'] <= 1000000*.005
    if mode == 'manual':
        assert entry['status'] == 'pending' and not state['trades']
        desk.approve(entry['id'])
    else:
        assert entry['status'] == 'filled'
    state = desk.store.read()
    position = state['positions']['005930']
    assert position['strategy_mode'] == 'intraday'
    assert position['stop_price'] < position['take_profit_price']
    assert position['expires_at'] > time.time()
    assert state['cash']['KRW'] < state['initial']['KRW']
    assert len(state['trades']) == 1 and len(state['history']) >= 2

    desk.provider.price = position['stop_price']-1
    desk.process_desk_exits()
    state = desk.store.read()
    exit_proposal = state['proposals'][-1]
    assert exit_proposal['exit_reason'] == '손절 조건'
    if mode == 'manual':
        assert exit_proposal['status'] == 'pending'
        desk.approve(exit_proposal['id'])
    else:
        assert exit_proposal['status'] == 'filled'
    desk.process_desk_exits()
    state = desk.store.read()
    assert not state['positions'] and len(state['trades']) == 2
    assert state['trades'][-1]['strategy_mode'] == 'intraday'
    assert state['trades'][-1]['realized'] == round(state['cash']['KRW']-state['initial']['KRW'], 2)
    assert state['cash']['USD'] == state['initial']['USD'] == 1000
    assert state['history'][-1]['KRW'] == state['cash']['KRW']

    # A new Store reads the committed ledger without resetting principal or fills.
    reopened = Store(desk.c.database_url, 'demo')
    try:
        restored = reopened.read()
        for key in ('initial', 'cash', 'positions', 'trades', 'strategy_settings'):
            assert restored[key] == state[key]
    finally:
        reopened.release()


def test_ai_selector_choice_drives_the_cycle(desk, monkeypatch):
    original = desk.agents.run
    seen = {}
    def run(role, context, generation):
        if role == 'selector':
            seen['candidates'] = [c['symbol'] for c in context['candidates']]
            seen['fields'] = set(context['candidates'][0])
            return dict(original(role, context, generation), symbol='000660')
        return original(role, context, generation)
    monkeypatch.setattr(desk.agents, 'run', run)
    desk.start()
    desk.cycle()
    run_record = desk.store.read()['runs'][-1]
    assert len(seen['candidates']) > 1 and '000660' in seen['candidates']
    assert {'spread_bps', 'return_20m_pct', 'volatility_1m_pct', 'position_quantity'} <= seen['fields']
    assert run_record['symbol'] == '000660' and run_record['selected_by'] == 'ai'


def test_user_requested_symbol_skips_ai_selection(desk, monkeypatch):
    original = desk.agents.run
    roles = []
    monkeypatch.setattr(desk.agents, 'run', lambda role, c, g: roles.append(role) or original(role, c, g))
    desk.start()
    desk.request_cycle('000660')
    desk.cycle()
    state = desk.store.read()
    assert 'selector' not in roles
    assert state['runs'][-1]['symbol'] == '000660' and state['runs'][-1]['selected_by'] == 'user'
    assert 'requested_symbol' not in state


def test_selector_failure_falls_back_to_rotation(desk, monkeypatch):
    original = desk.agents.run
    def run(role, context, generation):
        if role == 'selector':
            raise ProviderError('[Claude 실패] x')
        return original(role, context, generation)
    monkeypatch.setattr(desk.agents, 'run', run)
    desk.start()
    desk.cycle()
    state = desk.store.read()
    assert state['runs'][-1]['status'] == 'completed' and state['runs'][-1]['selected_by'] == 'server'
    assert any('AI 종목 선정 실패' in e.get('message', '') for e in state['events'])


def test_selection_compares_one_market_at_a_time(desk, monkeypatch):
    original = desk.agents.run
    seen = []
    def run(role, context, generation):
        if role == 'selector':
            seen.append((context['market'], {desk_symbols()[c['symbol']] for c in context['candidates']},
                         context['currency'], context['portfolio']['cash']))
        return original(role, context, generation)
    monkeypatch.setattr(desk.agents, 'run', run)
    desk.start()
    desk.cycle()
    first = desk.store.read()
    with desk.store.edit() as state:
        state['next_run'] = 0
    desk.cycle()
    second = desk.store.read()
    (m1, markets1, cur1, cash1), (m2, markets2, cur2, cash2) = seen
    assert markets1 == {m1} and markets2 == {m2} and m1 != m2
    assert {cur1, cur2} == {'KRW', 'USD'} and cash1 == first['cash'][cur1] and cash2 == second['cash'][cur2]
    assert first['last_market'] == m1 and second['last_market'] == m2
    assert second['runs'][-1]['reports'][0]['name'].endswith(('(국내)', '(미국)'))


def desk_symbols():
    from app.instruments import SYMBOLS
    return {k: v['market'] for k, v in SYMBOLS.items()}


def test_every_intraday_decision_is_recorded_for_scoring(desk):
    desk.set_execution('auto')
    desk.start()
    desk.cycle()
    state = desk.store.read()
    record = state['evaluations'][-1]
    assert record['run_id'] == state['runs'][-1]['id'] and record['stance'] == 'BUY'
    assert record['action'] == 'filled' and record['selected_by'] == 'ai' and record['cost_bps'] >= 40
    assert record['candidates'] and record['symbol'] not in record['candidates']
    public = desk.public_state()
    assert public['evaluation']['decisions'] == 1 and public['evaluation']['pending'] == 1
    assert 'candidates' not in public['evaluations'][-1]


def test_paper_proposals_leave_shadow_records_and_never_a_live_order(desk):
    desk.set_execution('auto')
    desk.start()
    desk.cycle()
    state = desk.store.read()
    shadow_order = state['shadow_orders'][-1]
    assert shadow_order['side'] == 'BUY' and shadow_order['source'] == 'ai' and shadow_order['sim_status'] == 'filled'
    assert shadow_order['symbol'] == state['runs'][-1]['symbol'] and shadow_order['order_type'] == 'LIMIT'
    assert 'protective' in shadow_order and isinstance(shadow_order['would_submit'], bool)
    position = state['positions'][shadow_order['symbol']]
    desk.provider.price = position['stop_price']-1
    desk.process_desk_exits()
    exit_record = desk.store.read()['shadow_orders'][-1]
    assert exit_record['side'] == 'SELL' and exit_record['source'] == 'exit'
    public = desk.public_state()
    assert 'shadow_orders' not in public and public['live']['config']['locked'] is True
    assert public['live']['shadow']['total'] == 2 and public['live']['config']['enabled'] is False


def test_a_shadow_failure_never_interrupts_paper_trading(desk, monkeypatch):
    import app.desk as desk_module

    def broken(*args, **kwargs):
        raise RuntimeError('shadow exploded')
    monkeypatch.setattr(desk_module, 'record_shadow', broken)
    desk.set_execution('auto')
    desk.start()
    desk.cycle()
    state = desk.store.read()
    assert state['runs'][-1]['status'] == 'completed' and state['proposals'][-1]['status'] == 'filled'
    assert state['positions'] and any('그림자 기록에 실패' in e['message'] for e in state['events'])


def test_shadow_can_be_switched_off(desk):
    from dataclasses import replace
    desk.c.live = replace(desk.c.live, shadow=False)
    desk.set_execution('auto')
    desk.start()
    desk.cycle()
    assert 'shadow_orders' not in desk.store.read() and desk.store.read()['positions']
