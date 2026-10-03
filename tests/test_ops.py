"""Operations: the automatic check of a fallback AI, and phone notifications of fills and warnings. A fake bridge and a fake
notification endpoint; no AI or notification service is contacted."""
import time

import httpx
import pytest

from app.config import Config
from app.engine import Engine
from app.notify import MAX_LINES, Notifier
from app.providers import DemoProvider
from app.store import Store

BRIDGE = 'http://bridge.test:8765'


def make(tmp_path, mode='toss'):
    cfg = Config(database_url='sqlite:///'+str(tmp_path/(mode+'.db')), mode=mode, password='test-password-123456',
                 session_secret='s'*40, toss_id='id', toss_secret='secret', gemini_key='', bridge_url=BRIDGE, bridge_token='t'*40,
                 providers='claude,codex')
    store = Store(cfg.database_url, cfg.mode)
    return Engine(cfg, store, DemoProvider()), store


@pytest.fixture
def engine(tmp_path):
    e, store = make(tmp_path)
    yield e
    store.release()


def bridge(monkeypatch, replies, calls):
    def post(url, **kwargs):
        provider = kwargs['json']['provider']
        calls.append(provider)
        reply = replies[provider]
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, json=reply, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx, 'post', post)


def test_a_fallback_that_has_not_answered_is_checked_once_it_is_usable(engine, monkeypatch):
    calls, t0 = [], time.time()
    bridge(monkeypatch, {'codex': {'ok': True, 'data': {'ok': True}, 'model': 'codex/gpt-6.1-sol'}}, calls)
    monkeypatch.setattr(engine, 'usable_providers', lambda: ['claude', 'codex'])
    engine.agents.answered['claude'] = t0+10*engine.CHECK_EVERY              # the main AI answers real requests all day
    engine.check_providers(now=t0)
    state = engine.store.read()
    assert calls == ['codex'] and state['ai_checks']['codex']['ok'] and state['ai_checks']['codex']['model'] == 'codex/gpt-6.1-sol'
    assert any('Codex 응답 확인: 정상' in e['message'] for e in state['events'])
    engine.check_providers(now=t0+60)                                           # at most every ten minutes
    engine.check_providers(now=t0+700)                                          # and a success holds for a day
    assert calls == ['codex']
    engine.check_providers(now=t0+engine.CHECK_EVERY+60)
    assert calls == ['codex', 'codex']


def test_a_failed_check_is_a_warning_and_is_retried_after_three_hours(engine, monkeypatch):
    calls, t0 = [], time.time()
    bridge(monkeypatch, {'codex': {'ok': False, 'message': 'model not supported'}}, calls)
    monkeypatch.setattr(engine, 'usable_providers', lambda: ['codex'])
    engine.check_providers(now=t0)
    check = engine.store.read()['ai_checks']['codex']
    assert not check['ok'] and check['message'] == 'model not supported'
    assert any(e['level'] == 'warning' and 'Codex 응답 확인: 실패' in e['message'] for e in engine.store.read()['events'])
    engine.check_providers(now=t0+3600)
    assert calls == ['codex']
    engine.check_providers(now=t0+engine.CHECK_RETRY+60)
    assert calls == ['codex', 'codex']


@pytest.mark.parametrize('reply, message', [
    ({'ok': True, 'data': {'ok': False}, 'model': 'm'}, '응답 형식이 예상과 다릅니다.'),
    ({'ok': False, 'exhausted': True, 'until': time.time()+3600, 'message': 'Codex 사용량 소진'}, 'Codex 사용량 소진'),
    (httpx.ConnectError('refused'), '중계 서비스에 연결하지 못했습니다.'),
])
def test_what_a_check_reports_when_it_does_not_work(engine, monkeypatch, reply, message):
    bridge(monkeypatch, {'codex': reply}, [])
    result = engine.agents.self_check('codex')
    assert not result['ok'] and result['message'] == message and isinstance(result['seconds'], int)


def test_a_real_answer_counts_and_nothing_is_checked_in_demo_mode(engine, monkeypatch, tmp_path):
    calls = []
    bridge(monkeypatch, {}, calls)
    monkeypatch.setattr(engine, 'usable_providers', lambda: ['claude', 'codex'])
    engine.agents.answered.update(claude=time.time(), codex=time.time())
    engine.check_providers(now=time.time())
    demo, store = make(tmp_path, 'demo')
    demo.check_providers(now=time.time())
    store.release()
    assert calls == []


def test_the_checks_outlive_a_new_experiment(engine):
    with engine.store.edit() as s:
        s['ai_checks'] = {'codex': {'ok': True, 'time': 1.0}}
    engine.new_experiment(1000, 10, 'next')
    assert engine.store.read()['ai_checks'] == {'codex': {'ok': True, 'time': 1.0}}


# ---- notifications ------------------------------------------------------------------------------------------------------------

def ledger(trades=(), events=()):
    return {'trades': list(trades), 'events': list(events)}


BUY = {'id': 'a', 'symbol': '005930', 'side': 'BUY', 'quantity': 2, 'price': 70035.0, 'currency': 'KRW'}
SELL = {'id': 'b', 'symbol': '005930', 'side': 'SELL', 'quantity': 2, 'price': 77000.0, 'currency': 'KRW', 'realized': 13500.4,
        'exit_reason': '익절 조건'}
WATCH = {'id': 'c', 'symbol': 'AAPL', 'side': 'BUY', 'quantity': 0.5, 'price': 200.1, 'currency': 'USD', 'entry_watch': 'w'}
OLD = {'time': 1, 'message': '예전 경고', 'level': 'warning'}


def test_the_first_look_only_remembers_then_new_fills_and_warnings_are_sent():
    sent = []
    post = lambda url, **kw: sent.append((url, kw['content'].decode(), kw['headers']))       # noqa: E731
    n = Notifier('https://ntfy.sh/a-long-secret-topic')
    assert n.poll(ledger([BUY], [OLD]), post) == [] and sent == []
    lines = n.poll(ledger([BUY, SELL, WATCH], [OLD, {'time': 2, 'message': '일일 손실 한도', 'level': 'warning'},
                                               {'time': 3, 'message': '정보', 'level': 'info'}]), post)
    assert lines == ['삼성전자 2주 모의 매도 77,000원 · 익절 조건 · 실현 +13,500원', 'Apple 0.5주 모의 매수 $200.10 · 조건 진입',
                     '⚠ 일일 손실 한도']
    assert sent == [('https://ntfy.sh/a-long-secret-topic', '\n'.join(lines), {'Title': 'Stock Lab'})]
    assert n.poll(ledger([BUY, SELL, WATCH]), post) == [] and len(sent) == 1


def test_notifications_are_off_without_a_url_and_a_failed_delivery_is_ignored(engine, monkeypatch):
    off = Notifier('')
    off.poll(ledger())
    assert not off.enabled and off.poll(ledger([BUY]), post=lambda *a, **k: 1/0) == ['삼성전자 2주 모의 매수 70,035원']

    def broken(url, **kw):
        raise httpx.ConnectError('down')
    on = Notifier('https://ntfy.sh/topic')
    on.poll(ledger())
    assert on.poll(ledger([BUY]), post=broken) == ['삼성전자 2주 모의 매수 70,035원']
    monkeypatch.setattr(engine.store, 'read', lambda: 1/0)
    engine.notify()                                                              # off: the ledger is not even read


def test_a_burst_is_cut_to_a_readable_message():
    sent = []
    n = Notifier('https://ntfy.sh/topic')
    n.poll(ledger())
    n.poll(ledger([dict(BUY, id=str(i)) for i in range(30)]), post=lambda url, **kw: sent.append(kw['content'].decode()))
    assert len(sent[0].split('\n')) == MAX_LINES


def test_a_real_answer_is_remembered_as_proof_that_the_provider_works(engine, monkeypatch):
    bridge(monkeypatch, {'claude': {'ok': True, 'data': {'summary': 's', 'stance': 'HOLD', 'quantity': 0, 'risks': []},
                                    'sources': [], 'model': 'claude-opus-5-5'}}, [])
    before = time.time()
    engine.agents.bridge('claude', 'critic', {'reports': []}, False, 'sys', '{}', {}, False)
    assert engine.agents.answered['claude'] >= before


# ---- re-checking a provider believed exhausted (2026-10-03: the limits had reset, the stored readings had not) ------------------

def test_a_provider_believed_exhausted_is_probed_every_three_hours(engine, monkeypatch):
    calls, sent, t0 = [], [], time.time()
    def post(url, **kwargs):
        calls.append(kwargs['json']['provider']); sent.append(kwargs['json'])
        return httpx.Response(200, json={'ok': True, 'data': {'ok': True}, 'model': 'codex/gpt-6.1-sol'}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx, 'post', post)
    states = {'claude': 'ok', 'codex': 'exhausted'}
    monkeypatch.setattr(engine.agents.gate, 'status', lambda p, data=None: {'state': states[p]})
    monkeypatch.setattr(engine, 'usable_providers', lambda: ['claude'])
    engine.agents.answered['claude'] = t0+10*engine.CHECK_EVERY
    engine.check_providers(now=t0)
    assert calls == ['codex'] and sent[0].get('probe') is True
    state = engine.store.read()
    assert state['ai_checks']['codex']['probe'] and any('Codex 사용량 재확인: 응답 정상' in e['message'] for e in state['events'])
    engine.check_providers(now=t0+700)
    assert calls == ['codex']                                                   # not again within three hours
    engine.check_providers(now=t0+engine.PROBE_EVERY+60)
    assert calls == ['codex', 'codex']
    states['codex'] = 'ok'
    engine.check_providers(now=t0+2*engine.PROBE_EVERY+120)
    assert all(not s.get('probe') for s in sent[2:])                            # usable again: no more probes
