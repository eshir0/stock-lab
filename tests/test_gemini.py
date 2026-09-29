import copy
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agents import Agents, DESK_SCHEMA, SCHEMA, api_error_message, link_evidence, validate_report
from app.config import Config, gemini_model_id
from app.main import create_app
from app.providers import ProviderError
from app.store import Store


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Tests must not contact an external service')
    monkeypatch.setattr(httpx, 'post', blocked)
    monkeypatch.setattr(httpx, 'get', blocked)


@pytest.fixture
def agent(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'gemini.db'), mode='toss',
                    password='test-password-123456', session_secret='test-session-secret-1234567890123456',
                    toss_id='test-toss-client', toss_secret='test-toss-secret',
                    gemini_key='test-gemini-key', model='gemini-2.5-flash', ai_daily_calls=30, providers='gemini')
    store = Store(config.database_url, config.mode)
    with store.edit() as state:
        state.update(running=True, generation=1)
    yield Agents(config, store)
    store.release()


def report(desk=False):
    result = {'summary': '확인된 입력으로 관망합니다.', 'stance': 'HOLD', 'quantity': 0, 'risks': []}
    if desk:
        result.update(target_weight_pct=0, stop_loss_pct=2, take_profit_pct=4,
                      max_holding_minutes=60, tasks=[], evidence=[])
    return result


def response_payload(value=None, *, finish='STOP', metadata=None):
    result = {'candidates': [{'finishReason': finish, 'content': {'parts': [
        {'text': json.dumps(report() if value is None else value, ensure_ascii=False)}]}}],
        'usageMetadata': {'promptTokenCount': 120, 'candidatesTokenCount': 80, 'totalTokenCount': 200}}
    if metadata is not None:
        result['candidates'][0]['groundingMetadata'] = metadata
    return result


def mock_post(monkeypatch, payload, status=200):
    requests = []
    def post(url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(status, json=payload, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx, 'post', post)
    return requests


@pytest.mark.parametrize('desk', [False, True])
@pytest.mark.parametrize('model,role,search,structured', [
    ('gemini-2.5-flash', 'fundamental', True, False),
    ('gemini-2.5-flash', 'news', True, False),
    ('gemini-2.5-flash', 'technical', False, True),
    ('gemini-3.8-flash', 'fundamental', True, True),
    ('gemini-3.8-flash', 'news', True, True),
    ('gemini-3.8-flash', 'technical', False, True),
])
def test_rest_request_auth_and_search_schema_compatibility(agent, monkeypatch, desk, model, role, search, structured):
    agent.c.model = '  models/'+model+'  '
    payload = response_payload(report(desk))
    requests = mock_post(monkeypatch, payload)
    context = {'strategy_mode': 'intraday' if desk else 'legacy', 'reports': []}
    result = agent.run(role, context, 1)
    assert len(requests) == 1
    url, options = requests[0]
    assert url == 'https://generativelanguage.googleapis.com/v1beta/models/'+model+':generateContent'
    assert agent.c.gemini_key not in url
    assert options['headers'] == {'x-goog-api-key': agent.c.gemini_key}
    body = options['json']
    assert json.loads(body['contents'][0]['parts'][0]['text']) == context
    assert agent.c.gemini_key not in json.dumps(body)
    assert body.get('tools') == ([{'google_search': {}}] if search else None)
    generation = body['generationConfig']
    schema = DESK_SCHEMA if desk else SCHEMA
    if structured:
        assert generation['responseMimeType'] == 'application/json'
        assert generation['responseJsonSchema'] == schema
    else:
        assert 'responseMimeType' not in generation and 'responseJsonSchema' not in generation
        assert json.dumps(schema, ensure_ascii=False) in body['systemInstruction']['parts'][0]['text']
    assert ('thinkingConfig' in generation) == model.startswith('gemini-2.5-flash')
    assert result['engine'] == model
    assert result['usage']['total_tokens'] == 200
    assert {key: result['usage'][key] for key in payload['usageMetadata']} == payload['usageMetadata']
    assert 'total_tokens' not in payload['usageMetadata']
    day = datetime.now(timezone.utc).date().isoformat()
    assert agent.store.read()['daily_ai'] == {day: 1}


@pytest.mark.parametrize('payload', [
    [], {}, {'candidates': []}, {'candidates': [None]},
    {'promptFeedback': {'blockReason': 'SAFETY'}},
    {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': 'not JSON'}]}}]},
    response_payload([]), response_payload({'summary': 'bad', 'stance': 'BUY', 'quantity': True, 'risks': []}),
    response_payload({'summary': 'bad', 'stance': 'INVALID', 'quantity': 1, 'risks': []}),
    response_payload(finish='MAX_TOKENS'), response_payload(finish='SAFETY'),
    response_payload(finish='BLOCKLIST'), response_payload(finish='PROHIBITED_CONTENT'),
    response_payload(finish='UNEXPECTED_TOOL_CALL'), response_payload(finish=None),
])
def test_incomplete_or_invalid_responses_fail_closed(agent, monkeypatch, payload):
    requests = mock_post(monkeypatch, payload)
    with pytest.raises(ProviderError):
        agent.run('technical', {'reports': []}, 1)
    state = agent.store.read()
    assert len(requests) == 1 and sum(state['daily_ai'].values()) == 1
    assert not state['trades'] and not state['proposals']


def test_invalid_intraday_strategy_fails_closed(agent, monkeypatch):
    malformed = report(desk=True)
    # Serialize a JSON-valid nonnumeric value; request/response parsing must still reject it.
    malformed['stop_loss_pct'] = 'Infinity'
    mock_post(monkeypatch, response_payload(malformed))
    with pytest.raises(ProviderError):
        agent.run('technical', {'strategy_mode': 'intraday', 'reports': []}, 1)
    assert not agent.store.read()['proposals']


def test_fenced_search_json_and_thought_parts_are_handled(agent, monkeypatch):
    payload = response_payload()
    payload['candidates'][0]['content']['parts'] = [
        {'text': 'internal thought must not be parsed', 'thought': True},
        {'text': '```json\n'+json.dumps(report(), ensure_ascii=False)+'\n```'},
    ]
    mock_post(monkeypatch, payload)
    assert agent.run('news', {'reports': []}, 1)['summary'] == report()['summary']


@pytest.mark.parametrize('retrieved,unsupported', [
    ('https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260928000001',
     'https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20250928000002'),
    ('https://example.com:8443/article?id=1', 'https://example.com:9443/article?id=1'),
    ('https://example.com/report;document=1', 'https://example.com/report;document=2'),
])
def test_distinct_document_urls_cannot_satisfy_evidence_gate(retrieved, unsupported):
    research = report(desk=True)
    research['evidence'] = [{'claim': '검색 결과에 없는 다른 문서에 관한 주장입니다.',
                             'source_url': unsupported, 'published_at': None}]
    chunks = [{'url': retrieved, 'title': 'Retrieved source'}]
    link_evidence(research, chunks, {})
    assert research['evidence'][0]['source_url'] == unsupported
    research = validate_report(research, 'fundamental', {}, chunks, desk=True)
    assert research['evidence'] == []
    research['role'] = 'fundamental'
    decision = report(desk=True)
    decision.update(stance='BUY', quantity=1, target_weight_pct=20)
    result = validate_report(decision, 'director', {'reports': [research]}, [], desk=True)
    assert result['stance'] == 'HOLD' and result['quantity'] == 0


def test_same_document_query_keeps_evidence():
    source = 'https://example.com/report?id=123'
    research = report(desk=True)
    research['evidence'] = [{'claim': '검색한 문서에 연결된 주장입니다.',
                             'source_url': source+'#section', 'published_at': None}]
    chunks = [{'url': source, 'title': 'Retrieved source'}]
    link_evidence(research, chunks, {})
    result = validate_report(research, 'fundamental', {}, chunks, desk=True)
    assert result['evidence'][0]['source_url'] == source


def test_display_html_is_not_sent_to_later_analysts(agent, monkeypatch):
    requests = mock_post(monkeypatch, response_payload())
    context = {'reports': [{'summary': 'Earlier analysis',
                            'search_entry_point': '<style>display only</style>'}]}
    agent.run('critic', context, 1)
    submitted = json.loads(requests[0][1]['json']['contents'][0]['parts'][0]['text'])
    assert submitted == {'reports': [{'summary': 'Earlier analysis'}]}
    assert context['reports'][0]['search_entry_point'] == '<style>display only</style>'


def test_grounded_claim_uses_original_chunk_indices():
    source = 'https://example.com/report?id=123'
    claim = '실제 검색 지원 구간과 연결되는 충분히 긴 주장입니다.'
    research = report(desk=True)
    research['evidence'] = [{'claim': claim, 'source_url': 'https://example.com/incorrect', 'published_at': None}]
    chunks = [None, {'url': source, 'title': 'Retrieved source'}]
    metadata = {'groundingSupports': [{'segment': {'text': claim}, 'groundingChunkIndices': [1]}]}
    link_evidence(research, chunks, metadata)
    result = validate_report(research, 'fundamental', {}, [chunks[1]], desk=True)
    assert result['evidence'][0]['source_url'] == source


@pytest.mark.parametrize('rendered', ['<div>Google Search suggestions</div>', '', None, {}, 'x'*100001])
def test_grounding_entry_point_is_bounded_and_type_checked(agent, monkeypatch, rendered):
    payload = response_payload(metadata={'searchEntryPoint': {'renderedContent': rendered}})
    mock_post(monkeypatch, payload)
    result = agent.run('news', {'reports': []}, 1)
    expected = rendered if isinstance(rendered, str) and len(rendered) <= 100000 else ''
    assert result['search_entry_point'] == expected


@pytest.mark.parametrize('status,code', [(400, 'INVALID_ARGUMENT'), (401, 'UNKNOWN'),
                                       (403, 'PERMISSION_DENIED'), (404, 'NOT_FOUND'),
                                       (429, 'RESOURCE_EXHAUSTED'), (500, 'UNKNOWN')])
def test_provider_errors_do_not_echo_sensitive_values(agent, monkeypatch, status, code):
    sensitive = agent.c.gemini_key+' '+agent.c.password+' '+agent.c.toss_secret
    payload = {'error': {'status': code, 'message': sensitive,
                         'details': [{'reason': sensitive, 'metadata': {'secret': sensitive}}]}}
    mock_post(monkeypatch, payload, status=status)
    with pytest.raises(ProviderError) as raised:
        agent.run('technical', {'reports': []}, 1)
    for value in (agent.c.gemini_key, agent.c.password, agent.c.toss_secret):
        assert value not in str(raised.value)
    assert f'HTTP {status}' in str(raised.value)


@pytest.mark.parametrize('exception', [httpx.ConnectError, httpx.ReadTimeout])
def test_network_errors_do_not_echo_sensitive_values(agent, monkeypatch, exception):
    def failed(*args, **kwargs):
        raise exception(agent.c.gemini_key)
    monkeypatch.setattr(httpx, 'post', failed)
    with pytest.raises(ProviderError) as raised:
        agent.run('technical', {'reports': []}, 1)
    assert agent.c.gemini_key not in str(raised.value)


def test_quota_error_does_not_claim_account_is_free():
    message = api_error_message(429, {'error': {'status': 'RESOURCE_EXHAUSTED'}})
    assert '무료' not in message


def test_daily_limit_blocks_request_without_spending_another_call(agent, monkeypatch):
    agent.c.ai_daily_calls = 1
    requests = mock_post(monkeypatch, response_payload())
    agent.run('technical', {'reports': []}, 1)
    with pytest.raises(ProviderError, match='오늘의 AI 호출 한도'):
        agent.run('technical', {'reports': []}, 1)
    assert len(requests) == 1 and sum(agent.store.read()['daily_ai'].values()) == 1


@pytest.mark.parametrize('running,generation', [(False, 1), (True, 2)])
def test_stopped_or_replaced_generation_does_not_reserve_a_call(agent, running, generation):
    with agent.store.edit() as state:
        state.update(running=running, generation=generation)
    with pytest.raises(ProviderError, match='중지된 분석'):
        agent.run('technical', {'reports': []}, 1)
    assert agent.store.read()['daily_ai'] == {}


@pytest.mark.parametrize('model', ['', '   ', 'models/', 'models/models/gemini-2.5-flash',
                                 'gemini 2.5', '../gemini-2.5-flash', 'gemini?key=secret'])
def test_invalid_model_is_rejected_before_reserving_or_calling(agent, model):
    agent.c.model = model
    assert not agent.c.ai_configured
    with pytest.raises(ProviderError, match='GEMINI_MODEL'):
        agent.run('technical', {'reports': []}, 1)
    assert agent.store.read()['daily_ai'] == {}


@pytest.mark.parametrize('key', ['', '   '])
def test_blank_api_key_is_not_configured(agent, key):
    agent.c.gemini_key = key
    assert not agent.c.ai_configured
    with pytest.raises(ProviderError, match='GEMINI_API_KEY'):
        agent.run('technical', {'reports': []}, 1)
    assert agent.store.read()['daily_ai'] == {}


def test_model_whitespace_and_optional_prefix_are_normalized():
    assert gemini_model_id('  models/gemini-2.5-flash  ') == 'gemini-2.5-flash'


@pytest.mark.parametrize('model', ['   ', 'gemini/invalid', 'gemini?key=secret'])
def test_start_rejects_invalid_model_without_starting_or_calls(tmp_path, model):
    config = Config(database_url='sqlite:///'+str(tmp_path/'api.db'), mode='toss',
                    password='test-password-123456', session_secret='test-session-secret-1234567890123456',
                    toss_id='test-toss-client', toss_secret='test-toss-secret',
                    gemini_key='test-gemini-key', model=model, providers='gemini')
    app = create_app(config, background=False, test=True)
    headers = {'X-Stocklab-Action': '1'}
    with TestClient(app) as client:
        assert client.post('/api/login', json={'password': config.password}, headers=headers).status_code == 200
        before = copy.deepcopy(app.state.store.read())
        response = client.post('/api/start', json={}, headers=headers)
        assert response.status_code == 409 and 'GEMINI_MODEL' in response.json()['detail']
        after = app.state.store.read()
        assert not after['running'] and after['generation'] == before['generation']
        assert after['daily_ai'] == {} and not after['runs']
        assert client.get('/api/state').json()['config']['ai_configured'] is False


@pytest.mark.parametrize('model,level,expected', [
    ('gemini-3.8-flash', 'high', {'thinkingLevel': 'high'}),
    ('gemini-3.8-flash', '', None),
    ('gemini-3.8-flash', 'extreme', None),
])
def test_gemini3_thinking_level(agent, monkeypatch, model, level, expected):
    agent.c.model, agent.c.gemini_thinking = model, level
    requests = mock_post(monkeypatch, response_payload())
    agent.run('technical', {'reports': []}, 1)
    generation = requests[0][1]['json']['generationConfig']
    assert generation.get('thinkingConfig') == expected
    assert generation['maxOutputTokens'] == (32768 if level == 'high' else 6144)


def test_search_off_skips_search_roles_without_spending_quota(agent, monkeypatch):
    agent.c.gemini_search = False
    requests = mock_post(monkeypatch, response_payload())
    with pytest.raises(ProviderError, match='Gemini 검색 불가'):
        agent.run('news', {'reports': []}, 1)
    assert requests == [] and agent.store.read()['daily_ai'] == {}
    agent.run('technical', {'reports': []}, 1)
    assert len(requests) == 1
