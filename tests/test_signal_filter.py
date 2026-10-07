"""Research signal filter (tools/data/research_plan.json Q1): in experiments with signal_filter='research', BUY signals that
lost money in both 2006-2018 and 2019-2026 for that market and instrument type no longer call the AI. Sell signals and the
owner's "지금 분석" are untouched; saved experiments keep acting on every rule."""
import time

import pytest
from fastapi.testclient import TestClient

from app import gate
from app.config import Config
from app.main import create_app
from app.risk import RiskError, normalize_settings

PASSWORD, SECRET = 'test-password-123456', 'test-secret-123456789012345678901234'
ACTION = {'X-Stocklab-Action': '1'}
NOW = 1_790_000_000.0


def test_the_drop_table_is_exactly_what_passed_the_registered_rule():
    assert gate.dropped('research', 'KR', False) == {'golden_cross', 'momentum', 'breakout'}
    assert gate.dropped('research', 'US', False) == {'momentum', 'breakout'}
    assert gate.dropped('research', 'US', True) == {'breakout'}
    assert gate.dropped('research', 'KR', True) == set()
    assert gate.dropped('all', 'KR', False) == set()


def test_a_name_with_only_dropped_buy_signals_is_not_analysed():
    v = gate.assess(held=False, signals={'momentum': 'BUY', 'breakout': 'BUY'}, last=None, price=100, now=NOW,
                    ignore={'momentum', 'breakout'})
    assert (v['eligible'], v['reason'], v['filtered']) == (False, 'filtered', ['momentum', 'breakout'])


def test_a_kept_signal_still_calls_and_only_it_is_reported():
    v = gate.assess(held=False, signals={'momentum': 'BUY', 'mean_reversion': 'BUY'}, last=None, price=100, now=NOW,
                    ignore={'momentum'})
    assert v['eligible'] and v['rules'] == ['mean_reversion'] and v['filtered'] == ['momentum']


def test_sell_signals_and_owner_requests_are_never_filtered():
    v = gate.assess(held=True, signals={'momentum': 'SELL'}, last=None, price=100, now=NOW, ignore={'momentum'})
    assert v['eligible'] and v['rules'] == ['momentum']
    v = gate.assess(held=False, signals={'momentum': 'BUY'}, last=None, price=100, now=NOW, forced=True, ignore={'momentum'})
    assert v['eligible'] and v['reason'] == 'requested'


def test_the_status_line_names_the_filtered_rules():
    line = gate.summary_line([{'name': '삼성전자', 'reason': 'filtered', 'rules': [], 'filtered': ['momentum']}])
    assert line == '삼성전자 연구로 제외한 매수 신호뿐(모멘텀)'


def test_saved_experiments_act_on_every_rule_and_bad_values_are_refused():
    assert normalize_settings({})['signal_filter'] == 'all'
    with pytest.raises(RiskError):
        normalize_settings({'signal_filter': 'some'})


def test_the_desk_applies_the_filter_per_market_and_type(tmp_path, monkeypatch):
    from test_month_desk import desk as make, run_cycle    # noqa
    gen = make.__wrapped__(tmp_path) if hasattr(make, '__wrapped__') else None
    if gen is None:
        pytest.skip('fixture not callable directly')
    engine = next(gen)
    try:
        with engine.store.edit() as s:
            s['strategy_settings']['signal_filter'] = 'research'
        monkeypatch.setattr('app.desk.signals', lambda rows: {'golden_cross': None, 'momentum': 'BUY', 'mean_reversion': 'HOLD', 'breakout': None})
        state = run_cycle(engine)
        kr = [c for c in state['desk_gate']['checked'] if c['symbol'] == '005930']
        assert kr and kr[0]['reason'] == 'filtered' and engine.calls == []
    finally:
        next(gen, None)


def test_new_experiments_default_to_the_research_v2_filter(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'x.db'), mode='demo', password=PASSWORD, session_secret=SECRET,
                    toss_id='', toss_secret='', gemini_key='')
    with TestClient(create_app(config, background=False, test=True)) as client:
        client.post('/api/login', json={'password': PASSWORD}, headers=ACTION)
        body = {'seed_krw': 1000000, 'seed_usd': 1000, 'name': 'x', 'strategy_mode': 'intraday', 'confirmation': '새 실험 시작'}
        assert client.post('/api/experiments', json=body, headers=ACTION).status_code == 200
        state = client.get('/api/state').json()
        assert state['strategy_settings']['signal_filter'] == 'research_v2' and state['verification']['signal_filter'] == 'research_v2'   # 2026-10-07
