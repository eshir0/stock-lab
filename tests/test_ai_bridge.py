import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bridge'))
import ai_bridge  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Cooldowns and readings are persisted; a test must never touch the real state file."""
    monkeypatch.setattr(ai_bridge, 'STATE_FILE', str(tmp_path/'bridge-state.json'))
    monkeypatch.setattr(ai_bridge, '_usage', {})
    monkeypatch.setattr(ai_bridge, '_cooldown', {})


def lines(*events):
    return [json.dumps(e, ensure_ascii=False) for e in events]


def test_claude_stream_collects_search_links_and_structured_output():
    links = [{'title': '기사', 'url': 'https://news.example.com/1'}]
    out = ai_bridge.parse_claude(lines(
        {'type': 'assistant', 'message': {'model': 'claude-sonnet', 'content': [
            {'type': 'tool_use', 'id': 's1', 'name': 'WebSearch', 'input': {'query': 'q'}}]}},
        {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed'}},
        {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 's1',
                                                  'content': 'Web search results\n\nLinks: '+json.dumps(links)}]}},
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': {'a': 1},
         'usage': {'input_tokens': 5, 'output_tokens': 7}}))
    assert out == {'data': {'a': 1}, 'sources': [{'url': 'https://news.example.com/1', 'title': '기사'}],
                   'usage': {'total_tokens': 12}, 'model': 'claude-sonnet',
                   'limits': {'status': 'allowed', 'type': '', 'windows': {}}}


def test_claude_rejected_rate_limit_is_exhaustion():
    with pytest.raises(ai_bridge.Exhausted) as exc:
        ai_bridge.parse_claude(lines({'type': 'rate_limit_event',
                                      'rate_limit_info': {'status': 'rejected', 'resetsAt': 1790680800}}))
    assert exc.value.until == 1790680800


def test_claude_limit_result_is_exhaustion():
    with pytest.raises(ai_bridge.Exhausted):
        ai_bridge.parse_claude(lines({'type': 'result', 'subtype': 'success', 'is_error': True,
                                      'result': "You've hit your limit · resets 3pm"}))


def test_codex_usage_limit_is_exhaustion_with_reset_time():
    message = ('You’ve hit your usage limit. Upgrade to Pro, visit https://chatgpt.com/codex/settings/usage '
               'to purchase more credits or try again at Oct 4th, 2026 9:04 AM.')
    with pytest.raises(ai_bridge.Exhausted) as exc:
        ai_bridge.parse_codex(lines({'type': 'thread.started'}, {'type': 'error', 'message': message},
                                    {'type': 'turn.failed', 'error': {'message': message}}))
    assert exc.value.until == ai_bridge.datetime(2026, 10, 4, 9, 4).timestamp()


def test_codex_agent_message_is_parsed():
    out = ai_bridge.parse_codex(lines(
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"stance": "HOLD"}'}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 3, 'output_tokens': 4, 'cached_input_tokens': 1}}))
    assert out == {'data': {'stance': 'HOLD'}, 'usage': {'total_tokens': 8}}


def test_codex_other_failure_is_not_exhaustion():
    with pytest.raises(RuntimeError):
        ai_bridge.parse_codex(lines({'type': 'turn.failed', 'error': {'message': 'stream disconnected'}}))


@pytest.mark.parametrize('url', ['http://127.0.0.1/x', 'http://192.168.1.117:8080/', 'file:///etc/passwd',
                                 'http://user:pw@example.com/', 'ftp://example.com/'])
def test_private_or_odd_urls_are_never_fetched(url):
    assert not ai_bridge.public_url(url)


def test_cli_arguments_grant_no_shell_or_file_tools():
    claude = ai_bridge.claude_args('sonnet', 'sys', {}, False)
    assert claude[claude.index('--tools')+1] == '' and '--strict-mcp-config' in claude
    claude = ai_bridge.claude_args('sonnet', 'sys', {}, True)
    assert claude[claude.index('--tools')+1] == 'WebSearch'
    codex = ai_bridge.codex_args('', '/tmp/s.json', '/tmp', True)
    assert codex[codex.index('-s')+1] == 'read-only'
    assert {'shell_tool', 'unified_exec'} <= {codex[i+1] for i, a in enumerate(codex) if a == '--disable'}


def test_cooldown_short_circuits_without_running_cli(monkeypatch):
    monkeypatch.setattr(ai_bridge, '_cooldown', {'codex': ai_bridge.time.time()+600})
    monkeypatch.setitem(ai_bridge.RUNNERS, 'codex', lambda *a: pytest.fail('CLI must not run'))
    status, body = ai_bridge.generate({'provider': 'codex', 'system': 's', 'prompt': 'p', 'schema': {}})
    assert status == 200 and body['exhausted'] and not body['ok']


def test_exhaustion_sets_cooldown(monkeypatch):
    monkeypatch.setattr(ai_bridge, '_cooldown', {})
    def exhausted(*args):
        raise ai_bridge.Exhausted('Claude 사용량 소진', ai_bridge.time.time()+900)
    monkeypatch.setitem(ai_bridge.RUNNERS, 'claude', exhausted)
    monkeypatch.setattr(ai_bridge, 'load_env', lambda: {})
    _, body = ai_bridge.generate({'provider': 'claude', 'system': 's', 'prompt': 'p', 'schema': {}})
    assert body['exhausted'] and ai_bridge.cooling('claude')


def test_codex_model_and_effort_are_passed():
    args = ai_bridge.codex_args('gpt-6-sol', '/tmp/s.json', '/tmp', False, 'medium')
    assert args[args.index('-m')+1] == 'gpt-6-sol' and 'model_reasoning_effort="medium"' in args
    assert not any('model_reasoning_effort' in a for a in ai_bridge.codex_args('', '/tmp/s', '/tmp', False, 'bogus'))


def test_claude_model_and_effort_are_passed():
    args = ai_bridge.claude_args('claude-opus-5-5', 'sys', {}, False, 'medium')
    assert args[args.index('--model')+1] == 'claude-opus-5-5' and args[args.index('--effort')+1] == 'medium'
    assert '--effort' not in ai_bridge.claude_args('sonnet', 'sys', {}, False, 'bogus')


REAL_EVENT = {'type': 'rate_limit_event', 'rate_limit_info': {
    'status': 'allowed', 'resetsAt': 1790739000, 'rateLimitType': 'five_hour', 'isUsingOverage': False,
    'unifiedWindows': {'five_hour': {'utilization': 0.17, 'resetsAt': 1790739000},
                       'seven_day': {'utilization': 0.02, 'resetsAt': 1791320400}}}}


def success(*events):
    return lines(*events, {'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': {'a': 1}, 'usage': {}})


def test_claude_reports_its_real_usage_windows_on_every_call():
    out = ai_bridge.parse_claude(success(REAL_EVENT))
    assert out['limits'] == {'status': 'allowed', 'type': 'five_hour', 'windows': {
        'five_hour': {'utilization': 0.17, 'resets_at': 1790739000}, 'seven_day': {'utilization': 0.02, 'resets_at': 1791320400}}}


def test_a_rejected_call_still_carries_the_usage_windows():
    event = {'type': 'rate_limit_event', 'rate_limit_info': dict(REAL_EVENT['rate_limit_info'], status='rejected')}
    with pytest.raises(ai_bridge.Exhausted) as exc:
        ai_bridge.parse_claude(lines(event))
    assert exc.value.limits['windows']['five_hour']['utilization'] == 0.17 and exc.value.limits['status'] == 'rejected'


@pytest.mark.parametrize('raw,expected', [(0.5, 0.5), (85, 0.85), (1, 1.0), (250, 1.0), (-3, 0.0), (True, None), ('0.4', None), (None, None)])
def test_utilization_is_normalised_to_a_fraction_and_junk_is_dropped(raw, expected):
    info = {'unifiedWindows': {'five_hour': {'utilization': raw, 'resetsAt': 5}}}
    windows = ai_bridge.claude_limits(info)['windows']
    if expected is None:
        assert windows == {}
    else:
        assert windows['five_hour']['utilization'] == expected


def test_readings_and_cooldowns_survive_a_restart(monkeypatch):
    limits = ai_bridge.claude_limits(REAL_EVENT['rate_limit_info'])
    ai_bridge.note_limits('claude', limits)
    ai_bridge.set_cooldown('codex', ai_bridge.time.time()+3600)
    ai_bridge.set_cooldown('claude', ai_bridge.time.time()+0)                       # min 60 s: expires almost at once
    monkeypatch.setattr(ai_bridge, '_usage', {})
    monkeypatch.setattr(ai_bridge, '_cooldown', {'claude': 1.0})                     # stale entry from an earlier life
    ai_bridge.load_state()
    assert ai_bridge.cooling('codex') and ai_bridge._usage['claude']['windows']['five_hour']['utilization'] == 0.17


def test_an_expired_cooldown_is_not_restored(monkeypatch):
    ai_bridge.set_cooldown('codex', ai_bridge.time.time()+3600)
    monkeypatch.setattr(ai_bridge, '_cooldown', {})
    monkeypatch.setattr(ai_bridge.time, 'time', lambda t=ai_bridge.time.time: t()+7200)
    ai_bridge.load_state()
    assert not ai_bridge.cooling('codex')


def test_a_missing_or_unwritable_state_file_never_breaks_a_request(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_bridge, 'STATE_FILE', str(tmp_path/'no-such-dir'/'state.json'))
    ai_bridge.note_limits('claude', ai_bridge.claude_limits(REAL_EVENT['rate_limit_info']))
    ai_bridge.set_cooldown('codex', ai_bridge.time.time()+600)
    ai_bridge.load_state()
    assert ai_bridge.cooling('codex')


def test_generate_records_the_usage_reading_of_a_successful_call(monkeypatch):
    limits = ai_bridge.claude_limits(REAL_EVENT['rate_limit_info'])
    monkeypatch.setitem(ai_bridge.RUNNERS, 'claude', lambda *a: {'data': {}, 'sources': [], 'usage': {}, 'model': 'm', 'limits': limits})
    monkeypatch.setattr(ai_bridge, 'load_env', lambda: {})
    _, body = ai_bridge.generate({'provider': 'claude', 'system': 's', 'prompt': 'p', 'schema': {}})
    assert body['ok'] and body['limits'] == limits and ai_bridge._usage['claude']['windows']['seven_day']['utilization'] == 0.02


def test_exhaustion_keeps_the_last_reading_and_the_cooldown(monkeypatch):
    limits = ai_bridge.claude_limits(dict(REAL_EVENT['rate_limit_info'], status='rejected'))
    def exhausted(*args):
        raise ai_bridge.Exhausted('Claude 사용량 소진', ai_bridge.time.time()+900, limits=limits)
    monkeypatch.setitem(ai_bridge.RUNNERS, 'claude', exhausted)
    monkeypatch.setattr(ai_bridge, 'load_env', lambda: {})
    ai_bridge.generate({'provider': 'claude', 'system': 's', 'prompt': 'p', 'schema': {}})
    assert ai_bridge.cooling('claude') and ai_bridge._usage['claude']['status'] == 'rejected'


class FakeRequest(ai_bridge.Handler):
    """The handler without a socket: enough to exercise authorization and the /usage route."""
    def __init__(self, path, auth):
        self.path, self.headers, self.sent = path, {'Authorization': auth}, None

    def _send(self, status, payload):
        self.sent = (status, payload)


TOKEN = 't'*40


@pytest.mark.parametrize('auth,ok', [('Bearer '+TOKEN, True), ('Bearer wrong', False), ('', False), ('Bearer é'+TOKEN, False), ('Bearer '+TOKEN[:-1], False)])
def test_usage_route_requires_the_bridge_token(monkeypatch, auth, ok):
    monkeypatch.setattr(ai_bridge, 'load_env', lambda: {'AI_BRIDGE_TOKEN': TOKEN})
    ai_bridge.note_limits('claude', ai_bridge.claude_limits(REAL_EVENT['rate_limit_info']))
    request = FakeRequest('/usage', auth)
    request.do_GET()
    status, payload = request.sent
    assert (status == 200) is ok
    if ok:
        assert payload['providers']['claude']['limits']['windows']['five_hour']['utilization'] == 0.17
        assert set(payload['providers']) == {'claude', 'codex'} and payload['providers']['codex']['cooldown_until'] is None
    else:
        assert status == 401 and 'providers' not in payload


def test_a_non_ascii_authorization_header_is_refused_not_an_error(monkeypatch):
    monkeypatch.setattr(ai_bridge, 'load_env', lambda: {'AI_BRIDGE_TOKEN': TOKEN})
    request = FakeRequest('/generate', 'Bearer é')
    request.headers['Content-Length'] = '2'
    request.do_POST()
    assert request.sent[0] == 401


def test_health_stays_unauthenticated_and_shows_only_cooldowns():
    request = FakeRequest('/health', '')
    request.do_GET()
    assert request.sent[0] == 200 and set(request.sent[1]) == {'ok', 'cooldown'}


# ---- model tiering ------------------------------------------------------------------------------------------------------------

def test_a_light_call_runs_on_the_light_model_when_one_is_set():
    env = {'CLAUDE_MODEL': 'claude-opus-5-5', 'CLAUDE_MODEL_LIGHT': 'claude-sonnet-5-5', 'CLAUDE_EFFORT': 'Medium',
           'CODEX_MODEL': 'gpt-6.1-sol', 'CODEX_EFFORT': 'high'}
    assert ai_bridge.pick(env, 'claude', 'light') == ('claude-sonnet-5-5', 'medium')
    assert ai_bridge.pick(env, 'claude', '') == ('claude-opus-5-5', 'medium')
    assert ai_bridge.pick(env, 'codex', 'light') == ('gpt-6.1-sol', 'high')                 # no light Codex model: the main one
    assert ai_bridge.pick(dict(env, CLAUDE_EFFORT_LIGHT='low'), 'claude', 'light') == ('claude-sonnet-5-5', 'low')
    assert ai_bridge.pick(dict(env, CLAUDE_EFFORT_LIGHT='low'), 'claude', '') == ('claude-opus-5-5', 'medium')
    assert ai_bridge.pick({}, 'claude', 'light') == ('sonnet', '') and ai_bridge.pick({}, 'codex') == ('', '')


def test_the_bridge_refuses_an_unknown_tier_and_passes_a_known_one_through(monkeypatch):
    assert ai_bridge.generate({'provider': 'claude', 'system': 's', 'prompt': 'p', 'schema': {}, 'tier': 'max'})[0] == 400
    seen = []
    monkeypatch.setitem(ai_bridge.RUNNERS, 'claude', lambda env, system, prompt, schema, search, tier: seen.append(tier) or
                        {'data': {}, 'sources': [], 'usage': {}, 'model': 'm', 'limits': None})
    monkeypatch.setattr(ai_bridge, 'cooling', lambda provider: None)
    monkeypatch.setattr(ai_bridge, 'note_limits', lambda provider, limits: None)
    assert ai_bridge.generate({'provider': 'claude', 'system': 's', 'prompt': 'p', 'schema': {}, 'tier': 'light'})[0] == 200
    assert ai_bridge.generate({'provider': 'claude', 'system': 's', 'prompt': 'p', 'schema': {}})[0] == 200
    assert seen == ['light', '']


def test_a_probe_passes_the_cooldown_and_an_answer_ends_it(monkeypatch):
    monkeypatch.setattr(ai_bridge, '_cooldown', {'codex': ai_bridge.time.time()+600})
    monkeypatch.setattr(ai_bridge, 'save_state', lambda: None)
    monkeypatch.setattr(ai_bridge, 'load_env', lambda: {})
    monkeypatch.setitem(ai_bridge.RUNNERS, 'codex', lambda *a: {'data': {'ok': True}, 'model': 'codex/x'})
    _, body = ai_bridge.generate({'provider': 'codex', 'system': 's', 'prompt': 'p', 'schema': {}, 'probe': True})
    assert body['ok'] and not ai_bridge.cooling('codex')


def test_a_failed_probe_keeps_the_cooldown(monkeypatch):
    until = ai_bridge.time.time()+600
    monkeypatch.setattr(ai_bridge, '_cooldown', {'codex': until})
    monkeypatch.setattr(ai_bridge, 'save_state', lambda: None)
    monkeypatch.setattr(ai_bridge, 'load_env', lambda: {})
    def still(*a):
        raise ai_bridge.Exhausted('소진', until+3600)
    monkeypatch.setitem(ai_bridge.RUNNERS, 'codex', still)
    _, body = ai_bridge.generate({'provider': 'codex', 'system': 's', 'prompt': 'p', 'schema': {}, 'probe': True})
    assert body['exhausted'] and ai_bridge.cooling('codex') >= until


# ---- direct usage readings (2026-10-03) -----------------------------------------------------------------------------------------

CODEX_BODY = {'plan_type': 'plus', 'rate_limit': {'allowed': True, 'limit_reached': False,
              'primary_window': {'used_percent': 2, 'limit_window_seconds': 18000, 'reset_at': 1791007249},
              'secondary_window': {'used_percent': 0, 'limit_window_seconds': 604800, 'reset_at': 1791594049}}}
CLAUDE_BODY = {'five_hour': {'utilization': 63.0, 'resets_at': '2026-10-03T04:00:00.660986+00:00'},
               'seven_day': {'utilization': 9.0, 'resets_at': '2026-10-06T21:00:00.661009+00:00'}, 'seven_day_opus': None}


def test_the_usage_payloads_become_readings():
    codex = ai_bridge.codex_reading(CODEX_BODY)
    assert codex['windows'] == {'five_hour': {'utilization': .02, 'resets_at': 1791007249},
                                'seven_day': {'utilization': 0.0, 'resets_at': 1791594049}} and codex['status'] == 'allowed'
    claude = ai_bridge.claude_reading(CLAUDE_BODY)
    assert claude['windows']['five_hour']['utilization'] == .63
    assert claude['windows']['seven_day']['resets_at'] == pytest.approx(1791320400.661009)
    assert ai_bridge.codex_reading({'rate_limit': None}) is None and ai_bridge.claude_reading({}) is None
    assert ai_bridge.codex_reading('x') is None and ai_bridge.claude_reading(None) is None


def test_room_again_ends_a_cooldown_and_a_full_window_starts_one(monkeypatch):
    monkeypatch.setattr(ai_bridge, 'save_state', lambda: None)
    monkeypatch.setattr(ai_bridge, '_usage', {})
    now = ai_bridge.time.time()
    monkeypatch.setattr(ai_bridge, '_cooldown', {'codex': now+9000})
    ai_bridge.apply_direct({'codex': ai_bridge.codex_reading(CODEX_BODY)})
    assert not ai_bridge.cooling('codex') and ai_bridge._usage['codex']['windows']['five_hour']['utilization'] == .02
    full = {'rate_limit': {'limit_reached': True, 'primary_window': {'used_percent': 100, 'reset_at': now+3600}}}
    ai_bridge.apply_direct({'codex': ai_bridge.codex_reading(full)})
    assert ai_bridge.cooling('codex') == pytest.approx(now+3600)


def test_read_direct_survives_missing_logins(tmp_path):
    assert ai_bridge.read_direct(home=str(tmp_path)) == {}


# ---- the source check connects to the address it checked (2026-10-03 review, CWE-918) -------------------------------------------

def test_a_second_dns_answer_is_never_used(monkeypatch):
    answers = iter([[(2, 1, 6, '', ('8.8.8.8', 443))], [(2, 1, 6, '', ('127.0.0.1', 443))]])
    monkeypatch.setattr(ai_bridge.socket, 'getaddrinfo', lambda *a, **k: next(answers))
    connected = []

    def fake_fetch(url, address, timeout):
        connected.append(address)
        return 200, None
    monkeypatch.setattr(ai_bridge, 'fetch_status', fake_fetch)
    assert ai_bridge.reachable('https://news.example/a', ai_bridge.time.monotonic()+10)
    assert connected == ['8.8.8.8']                                              # one lookup, pinned


def test_a_redirect_to_an_internal_host_is_refused(monkeypatch):
    def lookup(host, *a, **k):
        return [(2, 1, 6, '', ('8.8.8.8' if host == 'news.example' else '10.0.0.5', 443))]
    monkeypatch.setattr(ai_bridge.socket, 'getaddrinfo', lookup)
    hops = []

    def fake_fetch(url, address, timeout):
        hops.append(address)
        return 302, 'https://intranet.example/admin'
    monkeypatch.setattr(ai_bridge, 'fetch_status', fake_fetch)
    assert not ai_bridge.reachable('https://news.example/a', ai_bridge.time.monotonic()+10) and hops == ['8.8.8.8']


def test_bot_refusals_still_count_as_a_page_that_exists(monkeypatch):
    monkeypatch.setattr(ai_bridge.socket, 'getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('8.8.8.8', 443))])
    for status, ok in ((200, True), (403, True), (429, True), (404, False), (500, False)):
        monkeypatch.setattr(ai_bridge, 'fetch_status', lambda *a, s=status: (s, None))
        assert ai_bridge.reachable('https://news.example/a', ai_bridge.time.monotonic()+10) is ok
