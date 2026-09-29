import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bridge'))
import ai_bridge  # noqa: E402


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
                   'usage': {'total_tokens': 12}, 'model': 'claude-sonnet'}


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
