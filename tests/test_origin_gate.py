"""Writes must come from a page on our own origin. A proxy in front may rewrite Host; it must not break the login,
and it must not let another site in."""
import logging

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app

PASSWORD, SECRET = 'test-password-123456', 'test-secret-123456789012345678901234'
PUBLIC = 'https://stock.eshiro.net'
LAN = 'http://192.168.1.117:8080'


def make(tmp_path, monkeypatch, public=PUBLIC, base=PUBLIC):
    if public:
        monkeypatch.setenv('APP_PUBLIC_ORIGIN', public)
    else:
        monkeypatch.delenv('APP_PUBLIC_ORIGIN', raising=False)
    config = Config(database_url='sqlite:///'+str(tmp_path/'gate.db'), mode='demo', password=PASSWORD, session_secret=SECRET,
                    toss_id='', toss_secret='', gemini_key='')
    return TestClient(create_app(config, background=False, test=True), base_url=base)


def login(client, host, origin, **extra):
    headers = {'Host': host, 'X-Stocklab-Action': '1', **extra}
    if origin is not None:
        headers['Origin'] = origin
    return client.post('/api/login', json={'password': PASSWORD}, headers=headers)


@pytest.mark.parametrize('host', ['192.168.1.117:8080', '192.168.1.117', 'stock.eshiro.net:443', 'localhost:8080'])
def test_the_public_login_works_even_when_a_proxy_rewrites_host(tmp_path, monkeypatch, host):
    with make(tmp_path, monkeypatch) as client:
        r = login(client, host, PUBLIC)
        assert r.status_code == 200 and r.json() == {'ok': True}
        cookie = r.headers['set-cookie'].lower()
        assert 'httponly' in cookie and 'samesite=strict' in cookie and 'secure' in cookie      # the browser is on https
        # and the signed-in session can write through the same rewritten path
        assert client.post('/api/stop', headers={'Host': host, 'X-Stocklab-Action': '1', 'Origin': PUBLIC}).status_code == 200


@pytest.mark.parametrize('origin', ['https://evil.example', 'null', 'https://stock.eshiro.net.evil.example', 'http://stock.eshiro.net',
                                    'https://stock.eshiro.net:8443', 'https://STOCK.eshiro.net.', LAN])
def test_other_origins_stay_blocked_even_with_a_rewritten_host(tmp_path, monkeypatch, origin):
    with make(tmp_path, monkeypatch) as client:
        assert login(client, '192.168.1.117:8080', origin).status_code == 403 or origin == LAN


def test_the_lan_address_still_logs_in_from_its_own_origin_and_the_cookie_is_not_marked_secure(tmp_path, monkeypatch):
    with make(tmp_path, monkeypatch, base=LAN) as client:
        r = login(client, '192.168.1.117:8080', LAN)
        assert r.status_code == 200 and 'secure' not in r.headers['set-cookie'].lower()


def test_the_action_header_is_still_required(tmp_path, monkeypatch):
    with make(tmp_path, monkeypatch) as client:
        r = client.post('/api/login', json={'password': PASSWORD}, headers={'Host': '192.168.1.117:8080', 'Origin': PUBLIC})
        assert r.status_code == 403


def test_a_request_without_an_origin_header_keeps_working_for_non_browser_clients(tmp_path, monkeypatch):
    with make(tmp_path, monkeypatch) as client:
        assert login(client, 'stock.eshiro.net', None).status_code == 200


def test_without_a_configured_public_origin_nothing_is_trusted_implicitly(tmp_path, monkeypatch):
    with make(tmp_path, monkeypatch, public='', base=LAN) as client:
        assert login(client, '192.168.1.117:8080', PUBLIC).status_code == 403
        assert login(client, '192.168.1.117:8080', LAN).status_code == 200


def test_a_refusal_is_logged_once_with_headers_only(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger='uvicorn.error')
    with make(tmp_path, monkeypatch) as client:
        for _ in range(3):
            login(client, '192.168.1.117:8080', 'https://evil.example')
        login(client, '192.168.1.117:8080', None, **{'X-Stocklab-Action': '0'})
    lines = [r.getMessage() for r in caplog.records if 'write refused' in r.getMessage()]
    assert len(lines) == 2                                              # deduplicated per cause
    assert 'origin-mismatch' in lines[0] and "origin='https://evil.example'" in lines[0] and 'host=' in lines[0]
    assert 'missing-action-header' in lines[1]
    assert PASSWORD not in ' '.join(lines) and 'cookie' not in ' '.join(lines).lower()
