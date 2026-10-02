"""Server audit fixes (2026-10-02): security headers on every response, the login throttle behind a trusted tunnel, logout
that ends every session, conditional entries for names that left the list, safer experiment defaults, a quieter bridge and a
cleaner image. Throw-away apps and ledgers; no network."""
import pathlib
import time

import pytest
from fastapi.testclient import TestClient

from app import entry
from app.config import Config
from app.main import create_app
from app.risk import normalize_settings
from test_entry_desk import LEVEL, at, build, poll, watched

ROOT = pathlib.Path(__file__).resolve().parents[1]
PASSWORD, SECRET = 'test-password-123456', 'test-secret-123456789012345678901234'
ACTION = {'X-Stocklab-Action': '1'}


def app_client(tmp_path, monkeypatch, trusted='', db='h.db'):
    monkeypatch.setenv('TRUSTED_PROXY_IPS', trusted)
    config = Config(database_url='sqlite:///'+str(tmp_path/db), mode='demo', password=PASSWORD, session_secret=SECRET,
                    toss_id='', toss_secret='', gemini_key='')
    return TestClient(create_app(config, background=False, test=True))


def login(client, password=PASSWORD, ip=None):
    return client.post('/api/login', json={'password': password}, headers={**ACTION, **({'CF-Connecting-IP': ip} if ip else {})})


# ---- headers ------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('call', [lambda c: c.get('/api/state'), lambda c: c.post('/api/stop'),
                                  lambda c: c.post('/api/stop', headers={**ACTION, 'Origin': 'https://evil.example'})])
def test_the_gates_own_refusals_carry_the_security_headers(tmp_path, monkeypatch, call):
    with app_client(tmp_path, monkeypatch) as client:
        response = call(client)
        assert response.status_code in (401, 403)
        for header in ('X-Content-Type-Options', 'X-Frame-Options', 'Referrer-Policy', 'Cache-Control', 'Content-Security-Policy'):
            assert header in response.headers, header


# ---- the throttle behind a tunnel ---------------------------------------------------------------------------------------------

def test_behind_a_trusted_tunnel_each_visitor_has_their_own_failure_count(tmp_path, monkeypatch):
    with app_client(tmp_path, monkeypatch, trusted='testclient') as client:
        for _ in range(10):
            assert login(client, 'wrong-password', ip='203.0.113.7').status_code == 401
        assert login(client, 'wrong-password', ip='203.0.113.7').status_code == 429      # the stranger is stopped ...
        assert login(client, ip='198.51.100.20').status_code == 200                       # ... the owner is not
        assert login(client, 'wrong-password', ip='not-an-ip').status_code == 401         # a junk header counts as the tunnel


def test_an_untrusted_peer_cannot_claim_an_address(tmp_path, monkeypatch):
    with app_client(tmp_path, monkeypatch, trusted='') as client:
        for i in range(10):
            login(client, 'wrong-password', ip=f'203.0.113.{i}')
        assert login(client, ip='198.51.100.20').status_code == 429                       # one bucket: the header is ignored


# ---- logout -------------------------------------------------------------------------------------------------------------------

def test_logout_ends_every_session_and_the_end_outlives_a_restart(tmp_path, monkeypatch):
    with app_client(tmp_path, monkeypatch) as client:
        assert login(client).status_code == 200
        token = client.cookies.get('stocklab_session')
        assert client.get('/api/state').status_code == 200
        assert client.post('/api/logout', headers=ACTION).status_code == 200
        client.cookies.set('stocklab_session', token)                                      # a copied token
        assert client.get('/api/state').status_code == 401
        client.cookies.clear()
        assert login(client).status_code == 200 and client.get('/api/state').status_code == 200        # a new login works
    with app_client(tmp_path, monkeypatch) as again:                                       # the same ledger, a new process
        again.cookies.set('stocklab_session', token)
        assert again.get('/api/state').status_code == 401


def test_a_session_from_before_the_upgrade_still_works_until_a_logout(tmp_path, monkeypatch):
    import hashlib
    import hmac
    with app_client(tmp_path, monkeypatch) as client:
        value = f'{int(time.time()+3600)}:old-format-random'
        client.cookies.set('stocklab_session', value+'.'+hmac.new(SECRET.encode(), value.encode(), hashlib.sha256).hexdigest())
        assert client.get('/api/state').status_code == 200
        client.post('/api/logout', headers=ACTION)
        client.cookies.set('stocklab_session', value+'.'+hmac.new(SECRET.encode(), value.encode(), hashlib.sha256).hexdigest())
        assert client.get('/api/state').status_code == 401


def test_a_logout_still_counts_after_a_new_experiment(tmp_path):
    engine, store = build(tmp_path)
    with store.edit() as s:
        s['auth'] = {'revoked_before': 123.0}
    engine.stop()
    engine.new_experiment(1000, 10, 'next')
    assert store.read()['auth'] == {'revoked_before': 123.0}
    store.release()


# ---- conditional entries --------------------------------------------------------------------------------------------------------

@pytest.fixture
def day(tmp_path):
    engine, store = build(tmp_path)
    yield engine
    store.release()


def test_a_plan_for_a_name_that_left_the_list_is_cancelled_even_while_its_market_is_closed(day, monkeypatch):
    watched(day)
    day.provider.closed.add('005930')
    day.refresh()
    monkeypatch.setattr(day, 'focus_allows', lambda state, symbol: False)
    assert poll(day)['watches'][0]['status'] == 'cancelled'


def test_a_waiting_plan_keeps_its_name_in_the_quote_poll(day):
    now = time.time()
    with day.store.edit() as s:
        fields = {'type': 'pullback', 'level': 100.0, 'invalidate': 90.0, 'expires': now+3600, 'plan': {}}
        s['watches'].append(entry.make_watch(fields, symbol='NVDA', name='NVIDIA', market='US', currency='USD', horizon='month',
                                             reference=110.0, summary='', engine='', run_id='x', generation=1, now=now))
    day.refresh()
    assert 'NVDA' in day.provider.quote_symbols                                           # not in the fixed lineup, still quoted
    at(day, LEVEL)


# ---- safer defaults, a quieter bridge, a cleaner image ---------------------------------------------------------------------------

def test_leveraged_etfs_are_off_unless_asked_for(tmp_path, monkeypatch):
    assert normalize_settings({'include_leveraged_etfs': False})['include_leveraged_etfs'] is False     # the API always sends it
    assert 'type="checkbox">' in (ROOT/'app/static/index.html').read_text(encoding='utf-8').split('id="include-leveraged-etfs"')[1][:60]
    assert "$('include-leveraged-etfs').checked = false;" in (ROOT/'app/static/app.js').read_text(encoding='utf-8')
    with app_client(tmp_path, monkeypatch) as client:
        login(client)
        body = {'seed_krw': 1000000, 'seed_usd': 1000, 'name': 'x', 'strategy_mode': 'intraday', 'confirmation': '새 실험 시작'}
        assert client.post('/api/experiments', json=body, headers=ACTION).status_code == 200
        assert client.get('/api/state').json()['strategy_settings']['include_leveraged_etfs'] is False


def test_the_bridge_does_not_log_every_routine_usage_read():
    from bridge import ai_bridge
    assert not ai_bridge.should_log('/usage', '200') and not ai_bridge.should_log('/usage?x=1', 200)
    assert ai_bridge.should_log('/usage', '401') and ai_bridge.should_log('/generate', '200') and ai_bridge.should_log('/x', '404')


def test_the_image_gets_a_current_pip_and_no_backup_files():
    assert 'pip install --no-cache-dir --upgrade pip' in (ROOT/'Dockerfile').read_text(encoding='utf-8')
    assert '**/*.bak' in (ROOT/'.dockerignore').read_text(encoding='utf-8').split()
