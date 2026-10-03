"""Exit mode per experiment: 'target' sells everything at the take-profit (the rule saved experiments were started with),
'trail' (new experiments' default, chosen from the 2006-2026 backtest) keeps the position past the target and lets the
trailing stop or the holding limit end it."""
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app
from app.risk import RiskError, normalize_settings, trailed_stop
from test_month_desk import desk, open_position  # noqa: F401  (fixture)

PASSWORD, SECRET = 'test-password-123456', 'test-secret-123456789012345678901234'
ACTION = {'X-Stocklab-Action': '1'}


def test_saved_experiments_keep_selling_at_the_target():
    assert normalize_settings({})['exit_mode'] == 'target'
    assert normalize_settings({'exit_mode': 'trail'})['exit_mode'] == 'trail'
    with pytest.raises(RiskError):
        normalize_settings({'exit_mode': 'sometimes'})


def test_the_trailing_stop_is_capped_below_the_target_only_when_the_target_sells():
    pos = {'stop_price': 95.0, 'take_profit_price': 110.0, 'average': 100.0, 'trail_pct': 5.0}
    assert trailed_stop(pos, 130.0) == 109.99
    assert trailed_stop({**pos, 'exit_mode': 'trail'}, 130.0) == 123.5


def trail_mode(engine):
    with engine.store.edit() as s:
        s['strategy_settings']['exit_mode'] = 'trail'


def test_in_trail_mode_the_target_does_not_sell_and_the_trailing_stop_does(desk):
    trail_mode(desk)
    position = open_position(desk)
    assert position['exit_mode'] == 'trail'
    desk.provider.prices['005930'] = position['take_profit_price']+5000
    desk.process_desk_exits()
    state = desk.store.read()
    pos = state['positions']['005930']
    assert pos['target_hit'] and state['trades'][-1]['side'] == 'BUY'
    assert any('목표가 도달 · 팔지 않고' in e['message'] for e in state['events'])
    assert pos['stop_price'] > position['take_profit_price']-pos['high_water']*.06     # following the high past the target
    desk.provider.prices['005930'] = pos['stop_price']-100
    desk.process_desk_exits()
    sale = desk.store.read()['trades'][-1]
    assert (sale['side'], sale['exit_reason']) == ('SELL', '추적 손절(이익 보호)')
    assert sale['price'] > position['average']


def test_in_target_mode_nothing_changes(desk):
    position = open_position(desk)
    assert position['exit_mode'] == 'target'
    desk.provider.prices['005930'] = position['take_profit_price']+10
    desk.process_desk_exits()
    assert desk.store.read()['trades'][-1]['exit_reason'] == '익절 조건'


def test_the_ai_is_told_the_target_does_not_sell_only_in_trail_mode(desk):
    q = desk.provider.quote('005930')
    assert 'exit_rule' not in desk.order_constraints(desk.store.read(), '005930', q)
    trail_mode(desk)
    assert '팔지 않습니다' in desk.order_constraints(desk.store.read(), '005930', q)['exit_rule']


def test_new_experiments_default_to_trail_and_record_it_in_the_plan(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'x.db'), mode='demo', password=PASSWORD, session_secret=SECRET,
                    toss_id='', toss_secret='', gemini_key='')
    with TestClient(create_app(config, background=False, test=True)) as client:
        client.post('/api/login', json={'password': PASSWORD}, headers=ACTION)
        body = {'seed_krw': 1000000, 'seed_usd': 1000, 'name': 'x', 'strategy_mode': 'intraday', 'confirmation': '새 실험 시작'}
        assert client.post('/api/experiments', json=body, headers=ACTION).status_code == 200
        state = client.get('/api/state').json()
        assert state['strategy_settings']['exit_mode'] == 'trail' and state['verification']['exit_mode'] == 'trail'
        client.post('/api/stop', headers=ACTION)
        assert client.post('/api/experiments', json={**body, 'exit_mode': 'target'}, headers=ACTION).status_code == 200
        assert client.get('/api/state').json()['strategy_settings']['exit_mode'] == 'target'
        assert client.post('/api/experiments', json={**body, 'exit_mode': 'x'}, headers=ACTION).status_code == 422


def test_the_form_offers_trail_first():
    import pathlib
    html = (pathlib.Path(__file__).resolve().parents[1]/'app/static/index.html').read_text(encoding='utf-8')
    select = html.split('id="exit-mode"')[1].split('</select>')[0]
    assert select.index('value="trail"') < select.index('value="target"')
