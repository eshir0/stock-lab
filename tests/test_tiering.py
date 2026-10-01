"""Model tiering on the app side: the research roles ask the bridge for its light model, the deciding roles do not. A fake
bridge records the requests; no AI service is contacted."""
import httpx
import pytest

from app.agents import Agents
from app.config import Config
from app.store import Store


@pytest.fixture
def agent(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'tier.db'), mode='toss', password='test-password-123456',
                    session_secret='test-session-secret-1234567890123456', toss_id='id', toss_secret='secret', gemini_key='',
                    bridge_url='http://bridge.test:8765', bridge_token='t'*40, providers='claude')
    store = Store(config.database_url, config.mode)
    with store.edit() as state:
        state.update(running=True, generation=1)
    yield Agents(config, store)
    store.release()


def tiers(agent, monkeypatch, roles):
    sent = {}

    def post(url, **kwargs):
        sent[kwargs['json']['schema'].get('title', len(sent))] = kwargs['json'].get('tier')
        return httpx.Response(200, json={'ok': False, 'message': 'stop here'}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx, 'post', post)
    out = {}
    for role in roles:
        sent.clear()
        try:
            agent.run(role, {'strategy_mode': 'intraday', 'strategy_settings': {'horizon': 'month'}, 'reports': [], 'candidates': []}, 1)
        except Exception:
            pass
        out[role] = next(iter(sent.values()), None)
    return out


def test_research_roles_use_the_light_model_and_deciding_roles_the_main_one(agent, monkeypatch):
    assert tiers(agent, monkeypatch, ['planner', 'fundamental', 'technical', 'news', 'critic', 'director', 'selector', 'trend']) == {
        'planner': 'light', 'fundamental': 'light', 'technical': 'light', 'news': 'light',
        'critic': '', 'director': '', 'selector': '', 'trend': ''}


def test_tiering_can_be_switched_off_or_changed(agent, monkeypatch):
    agent.c.ai_light_roles = ()
    assert set(tiers(agent, monkeypatch, ['fundamental', 'director']).values()) == {''}
    agent.c.ai_light_roles = ('critic',)
    assert tiers(agent, monkeypatch, ['fundamental', 'critic']) == {'fundamental': '', 'critic': 'light'}


def test_the_default_roles():
    assert Config().ai_light_roles == ('planner', 'fundamental', 'technical', 'news')
