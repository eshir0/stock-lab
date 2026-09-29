import json

import httpx
import pytest

from app.agents import Agents
from app.config import Config
from app.providers import ProviderError
from app.store import Store

BRIDGE = 'http://bridge.test:8765'
GEMINI = 'generativelanguage.googleapis.com'


@pytest.fixture
def agent(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'chain.db'), mode='toss',
                    password='test-password-123456', session_secret='test-session-secret-1234567890123456',
                    toss_id='test-toss-client', toss_secret='test-toss-secret',
                    gemini_key='test-gemini-key', model='gemini-2.5-flash', ai_daily_calls=30,
                    bridge_url=BRIDGE, bridge_token='t'*40, providers='claude,codex,gemini')
    store = Store(config.database_url, config.mode)
    with store.edit() as state:
        state.update(running=True, generation=1)
    yield Agents(config, store)
    store.release()


def report():
    return {'summary': '관망합니다.', 'stance': 'HOLD', 'quantity': 0, 'risks': []}


def gemini_payload():
    return {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
        {'text': json.dumps(report(), ensure_ascii=False)}]}}], 'usageMetadata': {'totalTokenCount': 10}}


def fake_post(monkeypatch, bridge_replies):
    calls = []
    def post(url, **kwargs):
        if url.startswith(BRIDGE):
            provider = kwargs['json']['provider']
            calls.append(provider)
            assert kwargs['headers']['Authorization'] == 'Bearer '+'t'*40
            return httpx.Response(200, json=bridge_replies[provider], request=httpx.Request('POST', url))
        assert GEMINI in url
        calls.append('gemini')
        return httpx.Response(200, json=gemini_payload(), request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx, 'post', post)
    return calls


def test_claude_answers_first_without_using_gemini_quota(agent, monkeypatch):
    calls = fake_post(monkeypatch, {'claude': {'ok': True, 'data': report(), 'sources': [],
                                               'usage': {'total_tokens': 42}, 'model': 'claude-sonnet'}})
    result = agent.run('technical', {'reports': []}, 1)
    assert calls == ['claude']
    assert result['engine'] == 'Claude · claude-sonnet' and result['usage'] == {'total_tokens': 42}
    assert agent.store.read()['daily_ai'] == {}


def test_exhausted_claude_falls_back_to_codex(agent, monkeypatch):
    calls = fake_post(monkeypatch, {'claude': {'ok': False, 'exhausted': True, 'message': 'Claude 사용량 소진'},
                                    'codex': {'ok': True, 'data': report(), 'model': 'codex'}})
    result = agent.run('technical', {'reports': []}, 1)
    assert calls == ['claude', 'codex'] and result['engine'].startswith('Codex')


def test_both_cli_exhausted_falls_back_to_gemini_and_counts_quota(agent, monkeypatch):
    exhausted = {'ok': False, 'exhausted': True, 'message': '소진'}
    calls = fake_post(monkeypatch, {'claude': exhausted, 'codex': exhausted})
    result = agent.run('technical', {'reports': []}, 1)
    assert calls == ['claude', 'codex', 'gemini'] and result['engine'] == 'gemini-2.5-flash'
    assert sum(agent.store.read()['daily_ai'].values()) == 1


def test_invalid_cli_output_also_falls_through(agent, monkeypatch):
    calls = fake_post(monkeypatch, {'claude': {'ok': True, 'data': {'stance': 'MAYBE'}},
                                    'codex': {'ok': True, 'data': report()}})
    agent.run('technical', {'reports': []}, 1)
    assert calls == ['claude', 'codex']


def test_unreachable_bridge_falls_back_to_gemini(agent, monkeypatch):
    calls = []
    def post(url, **kwargs):
        calls.append(url)
        if url.startswith(BRIDGE):
            raise httpx.ConnectError('refused')
        return httpx.Response(200, json=gemini_payload(), request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx, 'post', post)
    assert agent.run('technical', {'reports': []}, 1)['engine'] == 'gemini-2.5-flash'
    assert len(calls) == 3


def test_all_failures_are_reported_in_order(agent, monkeypatch):
    agent.c.providers = 'claude,codex'
    fake_post(monkeypatch, {'claude': {'ok': False, 'exhausted': True, 'message': 'A'},
                            'codex': {'ok': False, 'message': 'B'}})
    with pytest.raises(ProviderError, match=r'\[Claude 사용량 소진\] A → \[Codex 실패\] B'):
        agent.run('technical', {'reports': []}, 1)


def test_search_sources_from_bridge_back_evidence(agent, monkeypatch):
    url = 'https://news.example.com/a?id=1'
    desk_report = dict(report(), target_weight_pct=0, stop_loss_pct=2, take_profit_pct=4, max_holding_minutes=60,
                       tasks=[], evidence=[{'claim': '확인된 사실', 'source_url': url, 'published_at': None},
                                           {'claim': '지어낸 사실', 'source_url': 'https://fake.example/x', 'published_at': None}])
    calls = fake_post(monkeypatch, {'claude': {'ok': True, 'data': desk_report,
                                               'sources': [{'url': url, 'title': '기사'}]}})
    result = agent.run('news', {'reports': [], 'strategy_mode': 'intraday'}, 1)
    assert calls == ['claude']
    assert [e['source_url'] for e in result['evidence']] == [url]


def test_provider_order_skips_unconfigured(agent):
    agent.c.bridge_token = 'short'
    assert agent.c.provider_order == ['gemini'] and agent.c.gemini_only
    agent.c.bridge_token, agent.c.gemini_key = 't'*40, ''
    assert agent.c.provider_order == ['claude', 'codex'] and not agent.c.gemini_only
