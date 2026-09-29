"""Switching AI before a limit is hit: the usage rule, and what a cycle does with it. No network, no real AI."""
import time
from types import SimpleNamespace

import pytest

from app import agents as agents_module
from app.agents import Agents
from app.config import Config
from app.engine import Engine
from app.providers import DemoProvider, ProviderError
from app.store import Store
from app.usage import UsageGate, clock_text, window_pct

NOW = 1_790_700_000.0
HOUR = 3600


def reading(five=None, week=None, status='allowed', now=NOW, five_reset=None, week_reset=None):
    windows = {}
    if five is not None:
        windows['five_hour'] = {'utilization': five, 'resets_at': now+2*HOUR if five_reset is None else five_reset}
    if week is not None:
        windows['seven_day'] = {'utilization': week, 'resets_at': now+5*24*HOUR if week_reset is None else week_reset}
    return {'status': status, 'type': 'five_hour', 'windows': windows}


def config(**over):
    base = dict(database_url='sqlite://', mode='toss', password='test-password-123456', session_secret='test-secret-123456789012345678901234',
                toss_id='id', toss_secret='secret', gemini_key='', bridge_url='http://bridge.invalid', bridge_token='t'*40,
                providers='claude,codex', ai_switch_pct=80.0)
    base.update(over)
    return Config(**base)


def gate(readings, **over):
    box = {'data': readings, 'calls': 0}
    def fetch():
        box['calls'] += 1
        return box['data']
    g = UsageGate(config(**over), fetch=fetch, clock=lambda: box.get('now', NOW))
    g.box = box
    return g


# ---- reading one provider ------------------------------------------------------------------------------------------------

def test_a_provider_below_the_switch_level_is_usable_and_shows_its_usage():
    s = gate({'claude': {'limits': reading(.17, .02)}}).status('claude')
    assert s['state'] == 'ok' and s['pct'] == 17.0 and s['windows']['seven_day']['pct'] == 2.0 and s['until'] is None


def test_no_reading_at_all_is_treated_as_usable():
    s = gate({}).status('claude')
    assert s['state'] == 'ok' and s['pct'] is None and s['windows'] == {}


@pytest.mark.parametrize('five,week,pct,label', [(.80, .02, 80.0, '5시간'), (.55, .93, 93.0, '주간'), (1.0, .10, 100.0, '5시간')])
def test_reaching_the_switch_level_in_any_window_marks_the_provider_high(five, week, pct, label):
    s = gate({'claude': {'limits': reading(five, week)}}).status('claude')
    assert s['state'] == 'high' and s['pct'] == pct and label in s['note'] and '전환 기준 80%' in s['note']


def test_just_under_the_level_is_still_usable():
    assert gate({'claude': {'limits': reading(.799)}}).status('claude')['state'] == 'ok'


def test_it_resumes_only_when_every_blocking_window_has_reset():
    five_reset, week_reset = NOW+2*HOUR, NOW+3*24*HOUR
    s = gate({'claude': {'limits': reading(.9, .85, five_reset=five_reset, week_reset=week_reset)}}).status('claude')
    assert s['state'] == 'high' and s['until'] == week_reset                       # both block: the later reset wins
    s = gate({'claude': {'limits': reading(.9, .10, five_reset=five_reset)}}).status('claude')
    assert s['until'] == five_reset and clock_text(five_reset) in s['note']


def test_a_window_that_has_already_reset_counts_as_empty():
    stale = reading(.95, .10, five_reset=NOW-60)                                   # the 95% reading is older than its reset
    s = gate({'claude': {'limits': stale}}).status('claude')
    assert s['state'] == 'ok' and s['windows']['five_hour']['pct'] == 0.0
    assert window_pct({'utilization': .5, 'resets_at': None}, NOW) == 50.0
    assert window_pct({'utilization': 'x'}, NOW) == 0.0 and window_pct({'utilization': 7}, NOW) == 100.0


def test_an_exhausted_provider_is_blocked_until_its_cooldown_ends():
    g = gate({'codex': {'cooldown_until': NOW+3*HOUR, 'limits': None}})
    s = g.status('codex')
    assert s['state'] == 'exhausted' and s['until'] == NOW+3*HOUR and '소진' in s['note']
    g.box['now'] = NOW+4*HOUR
    g.cache = None
    assert g.status('codex')['state'] == 'ok'


def test_the_switch_level_is_configurable():
    readings = {'claude': {'limits': reading(.90)}}
    assert gate(readings, ai_switch_pct=95.0).status('claude')['state'] == 'ok'
    assert gate(readings, ai_switch_pct=85.0).status('claude')['state'] == 'high'


# ---- choosing providers before a cycle -----------------------------------------------------------------------------------

def test_the_configured_order_is_kept_when_both_are_usable():
    usable, statuses, message = gate({'claude': {'limits': reading(.3)}, 'codex': {}}).plan()
    assert usable == ['claude', 'codex'] and message == '' and [s['name'] for s in statuses] == ['claude', 'codex']


def test_a_provider_at_the_switch_level_is_skipped_in_favour_of_the_next():
    usable, statuses, message = gate({'claude': {'limits': reading(.85)}, 'codex': {}}).plan()
    assert usable == ['codex'] and message == '' and statuses[0]['state'] == 'high'


def test_when_nothing_is_usable_the_message_names_each_reason_and_the_earliest_resume():
    reset = NOW+2*HOUR
    usable, _, message = gate({'claude': {'limits': reading(.9, five_reset=reset)}, 'codex': {'cooldown_until': NOW+4*24*HOUR}}).plan()
    assert usable == [] and message.startswith('AI 사용량 대기')
    assert 'Claude' in message and '5시간 창 90%' in message and 'Codex' in message and '소진' in message
    assert f'가장 빠른 재개 {clock_text(reset)}' in message


def test_gemini_is_not_in_the_default_chain_but_still_works_when_asked_for():
    assert Config().provider_order == [] or 'gemini' not in Config().provider_order
    only = config(providers='claude,codex,gemini', gemini_key='k', model='gemini-2.5-flash')
    assert UsageGate(only, fetch=lambda: {}).plan()[0] == ['claude', 'codex', 'gemini']
    assert config().provider_order == ['claude', 'codex']


def test_a_disabled_gate_does_not_look_at_the_bridge_at_all():
    g = gate({'claude': {'limits': reading(.99)}}, mode='demo')
    assert g.enabled is False
    assert g.plan() == (['claude', 'codex'], [], '') and g.summary() == {'enabled': False, 'providers': []} and g.box['calls'] == 0


# ---- freshness: the cache, an unreachable bridge, and readings taken from answers ----------------------------------------------

def test_the_bridge_is_asked_once_per_few_seconds():
    g = gate({'claude': {'limits': reading(.2)}})
    for _ in range(5):
        g.plan()
    assert g.box['calls'] == 1
    g.box['now'] = NOW+21
    g.plan()
    assert g.box['calls'] == 2


def test_an_unreachable_bridge_keeps_the_last_reading_and_is_not_hammered():
    g = gate({'claude': {'limits': reading(.9)}})
    assert g.plan()[0] == ['codex']
    g.box['data'] = None                                                            # the bridge went away
    g.box['now'] = NOW+30
    assert g.plan()[0] == ['codex']                                                 # still remembers Claude was high
    g.plan(), g.plan()
    assert g.box['calls'] == 2


def test_an_unreachable_bridge_at_first_means_unknown_and_usable():
    g = gate(None)
    assert g.plan()[0] == ['claude', 'codex']


def test_an_answer_updates_the_picture_immediately():
    g = gate({'claude': {'limits': reading(.3)}})
    assert g.plan()[0] == ['claude', 'codex']
    g.observe('claude', limits=reading(.91))
    assert g.plan()[0] == ['codex'] and g.box['calls'] == 1                          # no new fetch needed
    g.observe('codex', cooldown_until=NOW+HOUR)
    usable, _, message = g.plan()
    assert usable == [] and '소진' in message
    g.observe('codex', cooldown_until=NOW-10)                                       # a stale cooldown is ignored
    assert g.status('claude')['state'] == 'high'


def test_summary_is_what_the_dashboard_needs():
    summary = gate({'claude': {'limits': reading(.42, .07)}, 'codex': {'cooldown_until': NOW+HOUR}}).summary()
    assert summary['enabled'] and summary['switch_pct'] == 80.0 and summary['active'] == 'claude' and summary['usable'] == ['claude']
    by = {p['name']: p for p in summary['providers']}
    assert by['claude']['pct'] == 42.0 and by['codex']['state'] == 'exhausted'


# ---- the bridge client and the answer hooks in agents.py -----------------------------------------------------------------------

class Reply:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise agents_module.httpx.HTTPStatusError('x', request=None, response=None)

    def json(self):
        return self.payload


def test_the_usage_is_read_from_the_bridge_with_its_token(monkeypatch):
    seen = {}
    def fake_get(url, timeout, headers):
        seen.update(url=url, headers=headers, timeout=timeout)
        return Reply({'ok': True, 'providers': {'claude': {'limits': reading(.5)}}})
    monkeypatch.setattr(agents_module.httpx, 'get', fake_get)
    agents = Agents(config(bridge_url='http://172.17.0.1:8765/'), None)
    assert agents.fetch_usage() == {'claude': {'limits': reading(.5)}}
    assert seen['url'] == 'http://172.17.0.1:8765/usage' and seen['headers'] == {'Authorization': 'Bearer '+'t'*40} and seen['timeout'] <= 5


@pytest.mark.parametrize('reply', [Reply({}, 500), Reply(['not', 'an', 'object']), Reply({'providers': 'x'})])
def test_a_bad_answer_from_the_bridge_means_unknown_not_an_error(monkeypatch, reply):
    monkeypatch.setattr(agents_module.httpx, 'get', lambda *a, **k: reply)
    assert Agents(config(), None).fetch_usage() is None


def test_an_unconfigured_bridge_is_not_contacted(monkeypatch):
    monkeypatch.setattr(agents_module.httpx, 'get', lambda *a, **k: pytest.fail('must not call'))
    assert Agents(config(bridge_url='', bridge_token=''), None).fetch_usage() == {}


TREND_CTX = {'strategy_mode': 'intraday', 'candidates': [{'symbol': 'AAPL'}], 'max_picks': 1}
TREND_REPLY = {'market_view': '시장', 'themes': [], 'picks': [], 'avoid': [], 'risks': [],
               'evidence': [{'claim': '근거', 'source_url': 'https://n.example/a', 'published_at': None}]}


def test_every_answer_refreshes_the_usage_picture(monkeypatch):
    posted = []
    def fake_post(url, json, headers, timeout):
        posted.append(json['provider'])
        return Reply({'ok': True, 'data': TREND_REPLY, 'sources': [{'url': 'https://n.example/a', 'title': 't'}], 'usage': {},
                      'model': 'm', 'limits': reading(.88, now=time.time())})
    monkeypatch.setattr(agents_module.httpx, 'post', fake_post)
    agents = Agents(config(), None)
    agents.gate.fetch = lambda: {}
    agents.run('trend', TREND_CTX, 1)
    assert posted == ['claude'] and agents.gate.status('claude')['state'] == 'high'


def test_an_exhausted_answer_blocks_that_provider_at_once(monkeypatch):
    until = time.time()+7200
    monkeypatch.setattr(agents_module.httpx, 'post', lambda *a, **k: Reply({'ok': False, 'exhausted': True, 'until': until, 'message': 'x'}))
    agents = Agents(config(providers='claude'), None)
    agents.gate.fetch = lambda: {}
    with pytest.raises(ProviderError, match='사용량 소진'):
        agents.run('trend', TREND_CTX, 1)
    assert agents.gate.status('claude')['state'] == 'exhausted'


def test_a_lone_call_follows_the_gate_and_a_running_cycle_keeps_its_own_order(monkeypatch):
    posted = []
    monkeypatch.setattr(agents_module.httpx, 'post', lambda url, json, headers, timeout: posted.append(json['provider']) or Reply(
        {'ok': True, 'data': TREND_REPLY, 'sources': [{'url': 'https://n.example/a', 'title': 't'}], 'usage': {}, 'model': 'm'}))
    agents = Agents(config(), None)
    agents.gate.fetch = lambda: {'claude': {'limits': reading(.9, now=time.time())}}
    agents.run('trend', TREND_CTX, 1)                                              # no cycle: the gate says Codex
    agents.cycle_order = ['claude']                                                # a cycle that started on Claude
    agents.run('trend', TREND_CTX, 1)
    assert posted == ['codex', 'claude']


# ---- what an analysis cycle does ----------------------------------------------------------------------------------------------

@pytest.fixture
def desk(tmp_path):
    cfg = config(database_url='sqlite:///'+str(tmp_path/'gate.db'), mode='demo')
    store = Store(cfg.database_url, cfg.mode)
    engine = Engine(cfg, store, DemoProvider())
    engine.agents.gate.enabled = True                                              # demo answers are scripted, but the rule is under test
    engine.boot()
    engine.new_experiment(1000000, 1000, 'switch test', strategy_mode='intraday')
    engine.refresh()
    engine.readings = {}
    engine.agents.gate.fetch = lambda: engine.readings
    engine.calls = []
    real = engine.agents.run
    def spy(role, context, generation):
        engine.calls.append((role, tuple(engine.agents.cycle_order or ())))
        if engine.on_call:
            engine.on_call(len(engine.calls))
        return real(role, context, generation)
    engine.on_call = None
    engine.agents.run = spy
    yield engine
    store.release()


def run_cycle(engine):
    engine.agents.gate.cache = None
    with engine.store.edit() as s:
        s['next_run'] = 0
    engine.cycle()
    return engine.store.read()


def test_a_cycle_uses_the_first_provider_while_its_usage_is_low(desk):
    desk.readings = {'claude': {'limits': reading(.17, .02, now=time.time())}, 'codex': {'cooldown_until': time.time()+86400}}
    desk.start()
    state = run_cycle(desk)
    assert state['runs'][-1]['status'] == 'completed'
    assert {order for _, order in desk.calls} == {('claude',)} and len(desk.calls) == 7
    assert not any('이번 분석은' in e['message'] for e in state['events'])
    assert desk.agents.cycle_order is None                                          # cleaned up after the cycle


def test_before_a_cycle_starts_it_moves_to_the_next_ai_once_the_first_is_past_the_level(desk):
    now = time.time()
    desk.readings = {'claude': {'limits': reading(.85, .10, now=now)}, 'codex': {}}
    desk.start()
    state = run_cycle(desk)
    assert state['runs'][-1]['status'] == 'completed'
    assert {order for _, order in desk.calls} == {('codex',)}                       # the whole cycle on Codex
    switched = [e for e in state['events'] if e['message'].startswith('이번 분석은 Codex로 진행합니다')]
    assert switched and '5시간 창 85%' in switched[0]['message'] and switched[0]['level'] == 'warning'


def test_with_nothing_usable_the_cycle_waits_quietly_instead_of_failing(desk):
    now = time.time()
    desk.readings = {'claude': {'limits': reading(.9, five_reset=now+2*HOUR, now=now)}, 'codex': {'cooldown_until': now+4*86400}}
    desk.start()
    state = run_cycle(desk)
    assert state['runs'] == [] and desk.calls == []                                 # nothing started, nothing spent
    assert state['scheduler_status'].startswith('AI 사용량 대기') and '가장 빠른 재개' in state['scheduler_status']
    assert 55 <= state['next_run']-time.time() <= 61                                # look again in a minute
    assert not any(e['level'] == 'warning' and 'AI' in e['message'] and '실패' in e['message'] for e in state['events'])
    assert state['running'] is True


def test_it_carries_on_when_the_window_resets_and_says_it_went_back(desk):
    now = time.time()
    desk.readings = {'claude': {'limits': reading(.85, now=now)}, 'codex': {}}
    desk.start()
    run_cycle(desk)                                                                 # Codex this time
    desk.readings = {'claude': {'limits': reading(.85, now=now, five_reset=now-5)}, 'codex': {}}   # the 5-hour window has reset
    desk.calls.clear()
    state = run_cycle(desk)
    assert {order for _, order in desk.calls} == {('claude', 'codex')}
    assert any(e['message'].startswith('Claude로 되돌아왔습니다') for e in state['events'])


def test_a_cycle_that_started_on_claude_finishes_on_claude_even_if_it_crosses_the_level(desk):
    now = time.time()
    desk.readings = {'claude': {'limits': reading(.79, now=now)}, 'codex': {}}
    def cross(n):
        if n == 3:                                    # the bridge now reports 91%, as it would after this call
            desk.readings = {'claude': {'limits': reading(.91, now=now)}, 'codex': {}}
            desk.agents.gate.observe('claude', limits=reading(.91, now=now))
    desk.on_call = cross
    desk.start()
    state = run_cycle(desk)
    assert state['runs'][-1]['status'] == 'completed' and {order for _, order in desk.calls} == {('claude', 'codex')}
    desk.on_call = None
    desk.calls.clear()
    run_cycle(desk)
    assert {order for _, order in desk.calls} <= {('codex',), ()}                   # the NEXT cycle switches


def test_an_unreachable_bridge_does_not_stop_analysis(desk):
    desk.agents.gate.fetch = lambda: None
    desk.start()
    state = run_cycle(desk)
    assert state['runs'][-1]['status'] == 'completed' and {order for _, order in desk.calls} == {('claude', 'codex')}


def test_a_manual_analysis_request_is_gated_the_same_way(desk):
    now = time.time()
    desk.readings = {'claude': {'limits': reading(.95, now=now)}, 'codex': {'cooldown_until': now+86400}}
    desk.start()
    desk.request_cycle('005930')
    with desk.store.edit() as s:
        assert s['next_run'] <= time.time()
    state = run_cycle(desk)
    assert state['runs'] == [] and desk.calls == []


def test_the_legacy_five_role_strategy_is_gated_too(tmp_path):
    cfg = config(database_url='sqlite:///'+str(tmp_path/'legacy.db'), mode='demo')
    store = Store(cfg.database_url, cfg.mode)
    engine = Engine(cfg, store, DemoProvider())
    engine.agents.gate.enabled = True
    now = time.time()
    engine.agents.gate.fetch = lambda: {'claude': {'limits': reading(.9, now=now)}, 'codex': {'cooldown_until': now+86400}}
    engine.boot()
    engine.new_experiment(1000000, 1000, 'legacy gate', strategy_mode='legacy')
    engine.refresh()
    engine.start()
    with engine.store.edit() as s:
        s['next_run'] = 0
    engine.cycle()
    state = engine.store.read()
    assert state['runs'] == [] and state['scheduler_status'].startswith('AI 사용량 대기')
    store.release()


def test_the_morning_briefing_is_skipped_when_no_ai_is_usable(desk):
    desk.start()
    desk.readings = {'claude': {'limits': reading(.95, now=time.time())}, 'codex': {'cooldown_until': time.time()+86400}}
    desk.agents.gate.cache = None
    assert desk.focus_ai_wanted(desk.store.read()) is False
    desk.readings = {}
    desk.agents.gate.cache = None
    assert desk.focus_ai_wanted(desk.store.read()) is True


def test_the_dashboard_state_carries_the_ai_picture(desk):
    now = time.time()
    desk.readings = {'claude': {'limits': reading(.42, .07, now=now)}, 'codex': {'cooldown_until': now+86400}}
    ai = desk.public_state()['ai']
    assert ai['enabled'] and ai['switch_pct'] == 80.0 and ai['active'] == 'claude'
    assert [(p['name'], p['state']) for p in ai['providers']] == [('claude', 'ok'), ('codex', 'exhausted')]
    assert ai['providers'][0]['pct'] == 42.0


def test_demo_mode_never_gates(tmp_path):
    cfg = config(database_url='sqlite:///'+str(tmp_path/'demo.db'), mode='demo')
    store = Store(cfg.database_url, cfg.mode)
    engine = Engine(cfg, store, DemoProvider())
    assert engine.agents.gate.enabled is False and engine.public_state()['ai'] == {'enabled': False, 'providers': []}
    store.release()


def test_starting_no_longer_needs_a_gemini_key_when_the_bridge_is_configured(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    cfg = config(database_url='sqlite:///'+str(tmp_path/'start.db'), gemini_key='')
    app = create_app(cfg, background=False, test=True)
    with TestClient(app) as client:
        headers = {'X-Stocklab-Action': '1'}
        assert client.post('/api/login', json={'password': cfg.password}, headers=headers).status_code == 200
        assert client.post('/api/start', json={}, headers=headers).status_code == 200
        assert client.get('/api/state').json()['running'] is True
        client.post('/api/stop', json={}, headers=headers)


def test_starting_without_any_usable_ai_says_so(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    cfg = config(database_url='sqlite:///'+str(tmp_path/'none.db'), bridge_url='', bridge_token='', gemini_key='')
    app = create_app(cfg, background=False, test=True)
    with TestClient(app) as client:
        headers = {'X-Stocklab-Action': '1'}
        client.post('/api/login', json={'password': cfg.password}, headers=headers)
        response = client.post('/api/start', json={}, headers=headers)
        assert response.status_code == 409 and 'AI_BRIDGE' in response.json()['detail']
