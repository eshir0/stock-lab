import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app
from app.search_display import SEARCH_ENTRY_HEADER, register_search_display


ENTRY = ('<style>.chip { color: #1a73e8; }</style>\n'
         '<a class="chip" target="_blank" '
         'href="https://www.google.com/search?q=Samsung">삼성전자</a>')


class MemoryStore:
    def __init__(self, runs):
        self.runs = runs

    def read(self):
        return {'runs': copy.deepcopy(self.runs)}


def display_client(runs):
    app = FastAPI()
    register_search_display(app, MemoryStore(runs))
    return TestClient(app)


def run_entry(html=ENTRY):
    return {'id': 'run-1', 'status': 'completed',
            'reports': [{'role': 'news', 'search_entry_point': html}]}


def assert_isolated(response):
    assert response.headers['content-type'].startswith('text/html')
    assert response.headers['x-frame-options'] == 'SAMEORIGIN'
    directives = dict(item.strip().split(' ', 1)
                      for item in response.headers['content-security-policy'].split(';'))
    assert directives['default-src'] == "'none'"
    assert directives['style-src'] == "'unsafe-inline'"
    assert directives['base-uri'] == "'none'"
    assert directives['form-action'] == "'none'"
    assert directives['frame-ancestors'] == "'self'"
    assert set(directives['sandbox'].split()) == {
        'allow-popups', 'allow-popups-to-escape-sandbox'}
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'


def test_entry_point_preserves_provider_html_and_popup_links():
    with display_client([run_entry()]) as client:
        response = client.get('/api/search-entry/run-1/news')
    assert response.status_code == 200
    assert response.content == ENTRY.encode('utf-8')
    assert response.headers[SEARCH_ENTRY_HEADER] == '1'
    assert_isolated(response)


def test_direct_navigation_is_also_sandboxed_for_untrusted_html():
    html = '<script>parent.document.body.innerHTML="bad"</script>' + ENTRY
    with display_client([run_entry(html)]) as client:
        response = client.get('/api/search-entry/run-1/news')
    assert response.text == html
    assert_isolated(response)


@pytest.mark.parametrize('html', ['', '   ', None, {}, 'x' * 100001])
def test_missing_or_invalid_entry_point_returns_404(html):
    with display_client([run_entry(html)]) as client:
        assert client.get('/api/search-entry/run-1/news').status_code == 404


@pytest.mark.parametrize('path', ['missing/news', 'run-1/technical', 'run-1/unknown',
                                  'x' * 65 + '/news'])
def test_entry_point_requires_an_existing_run_and_role(path):
    with display_client([run_entry()]) as client:
        assert client.get('/api/search-entry/' + path).status_code == 404


def test_lookup_does_not_read_runs_outside_retained_window():
    runs = [run_entry()] + [{'id': 'later-' + str(n), 'reports': []} for n in range(40)]
    with display_client(runs) as client:
        assert client.get('/api/search-entry/run-1/news').status_code == 404


def test_search_entry_uses_application_auth_and_keeps_isolation_headers(tmp_path):
    config = Config(database_url='sqlite:///' + str(tmp_path / 'display.db'), mode='demo',
                    password='test-password-123456',
                    session_secret='test-secret-123456789012345678901234')
    app = create_app(config, background=False, test=True)
    with TestClient(app) as client:
        with app.state.store.edit() as state:
            state['runs'].append(run_entry())
        assert client.get('/api/search-entry/run-1/news').status_code == 401
        response = client.post('/api/login', json={'password': config.password},
                               headers={'x-stocklab-action': '1'})
        assert response.status_code == 200
        response = client.get('/api/search-entry/run-1/news')
        assert response.status_code == 200
        assert response.text == ENTRY
        assert SEARCH_ENTRY_HEADER not in response.headers
        assert_isolated(response)
        assert client.get('/').headers['x-frame-options'] == 'DENY'
