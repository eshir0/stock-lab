"""The USD account in won: dollars bought at the start's USD/KRW (plus the conversion spread), valued at today's rate (less
the spread), with the difference split into what the stocks and what the rate did."""
import json
import time

import pytest

from app import fx
from app.config import Config
from app.engine import Engine
from app.store import Store

NOW = 1_790_000_000.0


def write(folder, rate, at=NOW):
    (folder/'fx.json').write_text(json.dumps({'pair': 'USDKRW', 'rate': rate, 'time': at}))


def test_the_rate_must_be_sane_and_recent(tmp_path):
    assert fx.read(tmp_path, NOW) is None
    write(tmp_path, 1400.0)
    assert fx.read(tmp_path, NOW+3600) == {'rate': 1400.0, 'time': NOW}
    assert fx.read(tmp_path, NOW+5*86400) is None                               # older than a weekend
    write(tmp_path, 14.0)
    assert fx.read(tmp_path, NOW) is None
    assert fx.read('', NOW) is None


def test_1400_won_at_the_start_and_1380_now_lowers_the_won_value():
    record = fx.start(1000, {'rate': 1400.0}, NOW)
    assert record['krw_cost'] == round(1000*1400*1.0005)                         # 1,400,700 won with the 0.05% spread
    flat = fx.view(record, 1000, 1000, {'rate': 1380.0, 'time': NOW})
    assert flat['from_stocks_krw'] == 0 and flat['from_rate_krw'] == -20_000
    assert flat['krw_value'] == round(1000*1380*.9995) and flat['krw_pnl'] < -20_000   # the rate and both spreads
    up = fx.view(record, 1000, 1100, {'rate': 1380.0, 'time': NOW})
    assert up['from_stocks_krw'] == 140_000 and up['from_rate_krw'] == -22_000


def test_no_dollars_no_record_and_an_unreadable_rate_waits():
    assert fx.start(0, {'rate': 1400.0}, NOW) is None
    assert fx.start(500, None, NOW) == {'pending': True}
    assert fx.view({'pending': True}, 500, 500, {'rate': 1400.0, 'time': NOW}) is None


@pytest.fixture
def engine(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'fx.db'), mode='demo', password='x'*12, session_secret='y'*32,
                    evidence_dir=str(tmp_path))
    store = Store(config.database_url, config.mode)
    e = Engine(config, store)
    e.boot()
    yield e
    store.release()


def test_a_new_experiment_records_the_rate_and_the_dashboard_shows_the_won_view(engine, tmp_path):
    write(tmp_path, 1400.0, time.time())
    engine.new_experiment(1_000_000, 750, 'fx', strategy_mode='intraday', strategy_settings={'horizon': 'month'})
    assert engine.store.read()['fx']['start_rate'] == 1400.0
    write(tmp_path, 1380.0, time.time())
    view = engine.public_state()['fx_view']
    assert view['start_rate'] == 1400.0 and view['rate'] == 1380.0 and view['from_rate_krw'] == -15_000


def test_a_rate_that_arrives_later_is_used_once(engine, tmp_path):
    engine.new_experiment(1_000_000, 750, 'fx', strategy_mode='intraday', strategy_settings={'horizon': 'month'})
    assert engine.store.read()['fx'] == {'pending': True}
    write(tmp_path, 1390.0, time.time())
    engine.settle_fx()
    write(tmp_path, 1500.0, time.time())
    engine.settle_fx()
    state = engine.store.read()
    assert state['fx']['start_rate'] == 1390.0 and state['fx']['late']
