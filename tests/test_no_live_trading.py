"""Guards that keep this build incapable of live trading.

If one of these fails, the safety posture changed. Read docs/LIVE_TRADING.md before "fixing" the test:
turning live trading on is a deliberate, reviewed change that must update these guards on purpose.
"""
import ast
import pathlib

import pytest

from app import providers
from app.config import Config
from app.engine import Engine
from app.live import lock
from app.live.broker import Broker, DisabledBroker, create_broker
from app.live.errors import LiveTradingLocked
from app.live.lock import LIVE_TRADING_BUILD_ENABLED, LiveConfig
from app.providers import DemoProvider
from app.store import Store

APP = pathlib.Path(__file__).resolve().parents[1]/'app'
FORBIDDEN_FRAGMENTS = ('/api/v1/orders', 'conditional-orders', '/api/v1/accounts', '/api/v1/holdings',
                       'buying-power', 'sellable-quantity', '/api/v1/commissions', 'X-Tossinvest-Account')
HTTP_WRITE_METHODS = {'post', 'put', 'delete', 'patch', 'request', 'stream'}


def app_files(*suffixes):
    return [p for p in APP.rglob('*') if p.is_file() and p.suffix in suffixes and '__pycache__' not in p.parts]


def test_no_trading_or_account_route_is_referenced_anywhere_in_the_app():
    for path in app_files('.py', '.js', '.html'):
        text = path.read_text(encoding='utf-8')
        for fragment in FORBIDDEN_FRAGMENTS:
            assert fragment not in text, f'{fragment!r} appears in {path.relative_to(APP)}'


def test_toss_provider_only_posts_the_oauth_token_exchange():
    tree = ast.parse((APP/'providers.py').read_text(encoding='utf-8'))
    writes = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr in HTTP_WRITE_METHODS]
    assert len(writes) == 1 and writes[0].func.attr == 'post'
    assert '/oauth2/token' in ast.unparse(writes[0].args[0])


def test_only_the_ai_and_toss_modules_import_httpx():
    importers = set()
    for path in app_files('.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ''] if isinstance(node, ast.ImportFrom) else []
            if any(name == 'httpx' or name.startswith('httpx.') for name in names):
                importers.add(path.name)
    assert importers == {'agents.py', 'providers.py'}


def test_read_only_allow_list_matches_no_trading_or_account_path():
    for path in ('/api/v1/orders', '/api/v1/orders/abc', '/api/v1/orders/abc/cancel', '/api/v1/conditional-orders',
                 '/api/v1/conditional-orders/abc', '/api/v1/accounts', '/api/v1/holdings', '/api/v1/buying-power',
                 '/api/v1/sellable-quantity', '/api/v1/commissions', '/oauth2/token',
                 '/api/v1/stocks/005930/investor-trading/../../orders', '/api/v1/stocks/../orders'):
        assert not any(route.match(path) for route in providers.READ_ONLY_ROUTES), path


def test_modules_outside_the_live_package_may_only_use_the_lock_and_shadow_recorder():
    used = set()
    for path in app_files('.py'):
        if 'live' in path.relative_to(APP).parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and (node.module or '').startswith('live'):
                parts = node.module.split('.')
                used.update(parts[1:2] if len(parts) > 1 else {a.name for a in node.names})
    assert used <= {'lock', 'shadow'}, used


def test_build_lock_is_closed():
    assert LIVE_TRADING_BUILD_ENABLED is False


@pytest.mark.parametrize('value', ['on', '1', 'true', 'yes', 'ON', ' True '])
def test_the_environment_can_request_live_trading_but_never_enable_it(value):
    config = LiveConfig.from_env({'LIVE_TRADING': value, 'LIVE_ALLOW_BUY': 'on', 'LIVE_ALLOW_SELL': 'on'})
    assert config.requested and not config.enabled
    assert config.public()['locked'] and '무시' in config.reason


def test_app_config_stays_locked_even_when_the_environment_asks(monkeypatch):
    monkeypatch.setenv('LIVE_TRADING', 'on')
    assert Config().live.enabled is False


def test_broker_factory_only_ever_returns_the_disabled_stub():
    for enabled in (False, True):
        config = LiveConfig(requested=True, enabled=enabled)
        broker = create_broker(config)
        assert type(broker) is DisabledBroker and broker.can_trade is False


def test_disabled_broker_refuses_every_call_including_reads():
    broker = DisabledBroker()
    for name in ('normalize_price', 'place_order', 'cancel_order', 'find_orders', 'snapshot', 'list_protective',
                 'place_protective', 'cancel_protective'):
        with pytest.raises(LiveTradingLocked):
            getattr(broker, name)()
    assert isinstance(broker, Broker)


def test_engine_holds_no_broker_or_gate(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'guard.db'), mode='demo',
                    password='test-password-123456', session_secret='test-secret-123456789012345678901234',
                    toss_id='', toss_secret='', gemini_key='')
    store = Store(config.database_url, config.mode)
    try:
        engine = Engine(config, store, DemoProvider())
        assert not any(hasattr(engine, name) for name in ('broker', 'gate', 'order_gate', 'live_broker'))
    finally:
        store.release()


def test_lock_module_is_the_single_switch(monkeypatch):
    # Even if the constant were flipped, the request still needs the environment; both are required.
    monkeypatch.setattr(lock, 'LIVE_TRADING_BUILD_ENABLED', True)
    assert LiveConfig.from_env({}).enabled is False
    assert LiveConfig.from_env({'LIVE_TRADING': 'on'}).enabled is True
