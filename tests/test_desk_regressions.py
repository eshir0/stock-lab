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
        'planner', 'fundamental', 'technical', 'news', 'critic', 'director']
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
