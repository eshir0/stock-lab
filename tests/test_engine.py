import copy
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.engine import Engine, RuleError
from app.main import create_app
from app.providers import DemoProvider, ProviderError
from app.store import Store


class Fixed(DemoProvider):
    def __init__(self):
        self.stale = False
        self.closed = False
        self.price = 70000
        self.depth = 1000

    def quote(self, symbol):
        q = super().quote(symbol)
        price = self.price if symbol == '005930' else q['last']
        q.update(last=price, bid=price, ask=price, ask_size=self.depth, bid_size=self.depth)
        if self.stale:
            q['book_asof'] -= 60
        q['tradable'] = not self.closed
        return q


@pytest.fixture
def lab(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'test.db'), mode='demo',
                    password='test-password-123456', session_secret='test-secret-123456789012345678901234')
    store = Store(config.database_url, config.mode)
    e = Engine(config, store, Fixed())
    e.boot()
    e.refresh()
    yield e
    store.release()


def proposal(e, side='BUY', qty=1, symbol='005930'):
    e.start()
    q = e.provider.quote(symbol)
    with e.store.edit() as s:
        p = {'id': str(uuid.uuid4()), 'symbol': symbol, 'side': side, 'quantity': qty,
             'reference_price': q['ask'] if side == 'BUY' else q['bid'], 'expires': time.time()+180,
             'created': time.time(), 'generation': s['generation'], 'revision': s['revision'],
             'mode': e.c.mode, 'status': 'pending', 'summary': 'test', 'risks': []}
        s['proposals'].append(p)
    return p['id']


def test_approval_is_once_even_from_two_threads(lab):
    pid = proposal(lab)
    barrier = threading.Barrier(2)
    def approve():
        barrier.wait()
        return lab.approve(pid)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: approve(), range(2)))
    state = lab.store.read()
    assert len(state['trades']) == 1
    assert state['positions']['005930']['quantity'] == 1
    assert {r['status'] for r in results} <= {'filled', 'already_filled'}
    assert state['cash']['USD'] == 10000


def test_stop_invalidates_pending_and_preserves_positions(lab):
    lab.approve(proposal(lab))
    pid = proposal(lab)
    before = lab.store.read()['positions']
    lab.stop()
    with pytest.raises(RuleError):
        lab.approve(pid)
    assert lab.store.read()['positions'] == before


def test_stop_during_approval_quote_blocks_fill(lab):
    pid = proposal(lab)
    original = lab.provider.quote
    def race(symbol):
        lab.stop()
        return original(symbol)
    lab.provider.quote = race
    with pytest.raises(RuleError):
        lab.approve(pid)
    assert not lab.store.read()['trades']


def test_stop_during_agent_ignores_late_response(lab):
    lab.start()
    original = lab.agents.run
    def late(role, context, generation):
        lab.stop()
        return original(role, context, generation)
    lab.agents.run = late
    lab.cycle()
    s = lab.store.read()
    assert not s['proposals']
    assert s['runs'][0]['status'] == 'cancelled'


@pytest.mark.parametrize('fault', ['stale', 'closed', 'moved', 'currency', 'mode', 'future', 'depth', 'nan'])
def test_invalid_quote_never_fills(lab, fault):
    pid = proposal(lab)
    if fault == 'stale': lab.provider.stale = True
    if fault == 'closed': lab.provider.closed = True
    if fault == 'moved': lab.provider.price *= 1.02
    if fault == 'depth': lab.provider.depth = 0
    original = lab.provider.quote
    def mutate(symbol):
        q = original(symbol)
        if fault == 'currency': q['currency'] = 'USD'
        if fault == 'mode': q['mode'] = 'toss'
        if fault == 'future': q['book_asof'] += 90
        if fault == 'nan': q['ask'] = float('nan')
        return q
    lab.provider.quote = mutate
    with pytest.raises(RuleError): lab.approve(pid)
    assert not lab.store.read()['trades']
    assert lab.store.read()['cash']['KRW'] == 10000000


def test_revision_prevents_conflicting_orders(lab):
    a, b = proposal(lab), proposal(lab)
    lab.approve(a)
    with pytest.raises(RuleError): lab.approve(b)
    assert len(lab.store.read()['trades']) == 1


@pytest.mark.parametrize('qty', [-1, 0, 10001, True, 1.5])
def test_invalid_quantities(lab, qty):
    pid = proposal(lab, qty=qty)
    with pytest.raises(RuleError): lab.approve(pid)
    assert not lab.store.read()['trades']


def test_no_oversell_and_expiry(lab):
    pid = proposal(lab, side='SELL')
    with pytest.raises(RuleError): lab.approve(pid)
    pid = proposal(lab)
    with lab.store.edit() as s: s['proposals'][-1]['expires'] = time.time()-1
    with pytest.raises(RuleError): lab.approve(pid)


def test_cash_and_position_limits_rollback(lab):
    pid = proposal(lab, qty=20)
    with pytest.raises(RuleError): lab.approve(pid)
    with lab.store.edit() as s: s['cash']['KRW'] = 10
    pid = proposal(lab)
    with pytest.raises(RuleError): lab.approve(pid)
    assert lab.store.read()['cash']['KRW'] == 10


def test_restart_keeps_ledger_but_stops(lab):
    lab.approve(proposal(lab))
    proposal(lab)
    before = lab.store.read()
    lab.boot()
    after = lab.store.read()
    assert before['cash'] == after['cash'] and before['trades'] == after['trades']
    assert not after['running']
    assert not any(x['status'] == 'pending' for x in after['proposals'])


def test_liquidation_waits_closed_then_fills_once(lab):
    lab.approve(proposal(lab))
    proposal(lab)
    lab.liquidate()
    lab.provider.closed = True
    lab.process_liquidation()
    assert lab.store.read()['positions']
    assert lab.store.read()['liquidating']
    with pytest.raises(RuleError): lab.start()
    lab.provider.closed = False
    lab.process_liquidation()
    lab.process_liquidation()
    s = lab.store.read()
    assert not s['positions'] and not s['running'] and not s['liquidating']
    assert len(s['trades']) == 2


def test_stop_cancels_liquidation(lab):
    lab.approve(proposal(lab))
    lab.liquidate()
    lab.stop()
    lab.process_liquidation()
    assert lab.store.read()['positions']


def test_namespaces_separate(lab):
    lab.approve(proposal(lab))
    other = Store(lab.c.database_url, 'toss')
    assert not other.read()['positions']
    assert other.read()['cash']['KRW'] == 10000000
    other.release()


def test_demo_full_pipeline(lab):
    lab.start()
    lab.cycle()
    s = lab.store.read()
    assert len(s['runs'][0]['reports']) == 5
    assert len(s['proposals']) == 1
    lab.approve(s['proposals'][0]['id'])
    assert len(lab.store.read()['trades']) == 1


def test_http_auth_csrf_and_secret_exclusion(tmp_path):
    c = Config(database_url='sqlite:///'+str(tmp_path/'api.db'),mode='demo',
               password='test-password-123456',session_secret='test-secret-123456789012345678901234',
               toss_id='secret-client',toss_secret='secret-toss',gemini_key='secret-gemini')
    app = create_app(c, background=False, test=True)
    headers={'X-Stocklab-Action':'1'}
    with TestClient(app) as client:
        assert client.get('/api/state').status_code == 401
        assert client.post('/api/login',json={'password':c.password}).status_code == 403
        assert client.post('/api/login',json={'password':c.password},headers=headers).status_code == 200
        assert client.post('/api/start',json={},headers={**headers,'Origin':'https://evil.example'}).status_code == 403
        assert client.post('/api/start',json={},headers=headers).status_code == 200
        output=client.get('/api/state').text
        for secret in (c.password,c.session_secret,c.toss_id,c.toss_secret,c.gemini_key):
            assert secret not in output
        assert client.post('/api/liquidate',json={'confirmation':'wrong'},headers=headers).status_code == 409
        assert client.post('/api/logout',json={},headers=headers).status_code == 200
        assert client.get('/api/state').status_code == 401


def test_partial_sale_realized_matches_cash_change(lab):
    # Direct accounting test on a detached state, not a user's account or approval.
    s = lab.store.read()
    initial = s['cash']['KRW']
    lab.provider.price = 70000.13
    q = lab.provider.quote('005930')
    lab.fill(s, '005930', 'BUY', 3, q, 'test-buy')
    lab.provider.price = 70250.19
    q = lab.provider.quote('005930')
    first = lab.fill(s, '005930', 'SELL', 1, q, 'test-sell-1')
    last = lab.fill(s, '005930', 'SELL', 2, q, 'test-sell-2')
    assert not s['positions']
    assert round(first['realized']+last['realized'], 2) == round(s['cash']['KRW']-initial, 2)


def test_session_closure_rechecked_at_fill(lab):
    q = lab.quote_for_trade('005930')
    q['session_end'] = time.time()-1
    with pytest.raises(RuleError, match='거래 가능 시간'):
        lab.fill(lab.store.read(), '005930', 'BUY', 1, q, 'test')


def test_stale_other_holding_blocks_new_buy(lab):
    s = lab.store.read()
    s['positions']['000660'] = {'quantity': 1, 'average': 180000, 'cost_basis': 180000}
    s['quotes']['000660']['asof'] -= 60
    before = copy.deepcopy(s['cash'])
    with pytest.raises(RuleError, match='보유 종목의 최신'):
        lab.fill(s, '005930', 'BUY', 1, lab.provider.quote('005930'), 'test')
    assert s['cash'] == before and not s['trades']


def test_delayed_quote_does_not_regress_market_timestamps(lab):
    before = lab.store.read()['quotes']['005930']
    delayed = copy.deepcopy(before)
    delayed.update(received=before['received']+1, book_asof=before['book_asof']-1, last=1)
    lab.provider.quotes = lambda: {'005930': delayed}
    lab.refresh()
    assert lab.store.read()['quotes']['005930'] == before
