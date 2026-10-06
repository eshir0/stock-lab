"""Evidence packs from the history archive reach the AI desk: reading and freshness, the for/against summary, the analysis
context, the decision and trade records, the scorecard split, the prompts and the experiment setting."""
import json
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import agents, evidence, scorecard, verification
from app.config import Config
from app.evidence import EvidenceStore
from app.main import create_app
from app.risk import RiskError, normalize_settings
from test_month_desk import desk, run_cycle  # noqa: F401  (fixture)

PASSWORD, SECRET = 'test-password-123456', 'test-secret-123456789012345678901234'
ACTION = {'X-Stocklab-Action': '1'}


def pack(symbol='005930', built=None, group=(0.4, 0.2), own=0.3, analog=(1.5, 20)):
    return {'symbol': symbol, 'name': 'x', 'market': 'KR', 'etf': False, 'as_of_bar': datetime.now().date().isoformat(),
            'built_at': (built or datetime.now()).isoformat(timespec='seconds'),
            'signals_now': {'momentum': 'BUY'},
            'group_base_rates': {'momentum': {'develop_2006_2018': {'mean_pct': group[0], 'ci95_pct': [-.1, .9]},
                                              'holdout_2019_2026': {'mean_pct': group[1]}, 'dropped_by_site_filter': False}},
            'own_history': {'momentum': {'replay_site_exit': {'n': 40, 'mean_pct': own}, 'forward_21d': {'n': 80, 'mean_pct': 1.0}}},
            'analogs': {'rules': ['momentum'], 'forward_21d': {'n': analog[1], 'mean_pct': analog[0]}},
            'regime': {'vix': 16}, 'now': {'close': 1}}


def write(folder, p):
    (folder/f'{p["symbol"]}.json').write_text(json.dumps(p), encoding='utf-8')


def test_the_store_reads_fresh_packs_for_the_right_symbol_only(tmp_path):
    store = EvidenceStore(tmp_path)
    assert store.get('005930') is None                                         # missing
    write(tmp_path, pack())
    assert store.get('005930')['symbol'] == '005930'
    (tmp_path/'000660.json').write_text(json.dumps(pack('005930')))
    assert store.get('000660') is None                                         # another symbol's pack
    write(tmp_path, pack('AAPL', built=datetime.fromtimestamp(time.time()-5*86400)))
    assert store.get('AAPL') is None                                           # too old
    (tmp_path/'MSFT.json').write_text('{broken')
    assert store.get('MSFT') is None and store.get('../etc') is None
    assert EvidenceStore('').get('005930') is None


def test_the_summary_counts_the_archive_numbers():
    assert evidence.summary(pack(), ['momentum'])['sign'] == 'for'
    assert evidence.summary(pack(group=(-0.5, -0.2), own=-0.1, analog=(-1, 30)), ['momentum'])['sign'] == 'against'
    assert evidence.summary(pack(group=(-0.5, 0.2), own=None, analog=(1, 3)), ['momentum'])['sign'] == 'mixed'   # few analogs ignored
    assert evidence.summary(None, ['momentum']) == {'sign': 'none'}
    assert evidence.for_ai(None, [])['available'] is False


def test_an_analysis_carries_the_pack_and_records_its_verdict(desk, tmp_path, monkeypatch):
    write(tmp_path, pack())
    desk.evidence = EvidenceStore(tmp_path)
    with desk.store.edit() as s:
        s['strategy_settings']['evidence'] = 'on'
    monkeypatch.setattr('app.desk.signals', lambda rows: {'golden_cross': None, 'momentum': 'BUY', 'mean_reversion': 'HOLD', 'breakout': None})
    state = run_cycle(desk)
    director = next(ctx for role, ctx in desk.contexts if role == 'director' and ctx['symbol'] == '005930')
    assert director['evidence']['available'] and director['evidence']['triggered_rules'][0]['rule'] == 'momentum'
    run = next(r for r in state['runs'] if r['symbol'] == '005930')
    assert run['evidence']['sign'] == 'for'
    entry = next(e for e in state['evaluations'] if e['symbol'] == '005930')
    assert entry['evidence']['sign'] == 'for'
    buy = next(t for t in state['trades'] if t['symbol'] == '005930' and t['side'] == 'BUY')
    assert buy['evidence'] == 'for'


def test_without_the_setting_nothing_is_added(desk, tmp_path, monkeypatch):
    write(tmp_path, pack())
    desk.evidence = EvidenceStore(tmp_path)
    monkeypatch.setattr('app.desk.signals', lambda rows: {'golden_cross': None, 'momentum': 'BUY', 'mean_reversion': 'HOLD', 'breakout': None})
    state = run_cycle(desk)
    assert all('evidence' not in ctx for _, ctx in desk.contexts)
    assert all('evidence' not in r for r in state['runs'])


def test_the_prompts_change_only_with_a_pack_and_the_fingerprint_never_does():
    base = agents.desk_prompts('month')['director']
    assert agents.with_evidence(base, 'director', {}) == base
    assert '과거 20년' in agents.with_evidence(base, 'director', {'evidence': {'available': True}})
    assert '과거 근거 요약' in agents.with_evidence('x', 'selector', {'candidates': [{'evidence': {'summary': {}}}]})
    assert '과거 20년' not in json.dumps(agents.desk_prompts('month'), ensure_ascii=False)


def test_the_scorecard_splits_trips_by_the_evidence_of_their_buy():
    trades = [{'symbol': '005930', 'side': 'BUY', 'quantity': 1, 'price': 100, 'fee': 0, 'time': 1, 'evidence': 'for'},
              {'symbol': '005930', 'side': 'SELL', 'quantity': 1, 'price': 110, 'realized': 10, 'time': 2},
              {'symbol': '000660', 'side': 'BUY', 'quantity': 1, 'price': 100, 'fee': 0, 'time': 3, 'evidence': 'against'},
              {'symbol': '000660', 'side': 'SELL', 'quantity': 1, 'price': 90, 'realized': -10, 'time': 4}]
    g = scorecard.report(trades)['groups']
    assert g['evidence_for']['count'] == 1 and g['evidence_against']['count'] == 1 and g['evidence_mixed']['count'] == 0


def test_saved_experiments_go_without_and_new_ones_with_it(tmp_path):
    assert normalize_settings({})['evidence'] == 'off'
    with pytest.raises(RiskError):
        normalize_settings({'evidence': 'maybe'})
    config = Config(database_url='sqlite:///'+str(tmp_path/'x.db'), mode='demo', password=PASSWORD, session_secret=SECRET,
                    toss_id='', toss_secret='', gemini_key='')
    with TestClient(create_app(config, background=False, test=True)) as client:
        client.post('/api/login', json={'password': PASSWORD}, headers=ACTION)
        body = {'seed_krw': 1000000, 'seed_usd': 1000, 'name': 'x', 'strategy_mode': 'intraday', 'confirmation': '새 실험 시작'}
        assert client.post('/api/experiments', json=body, headers=ACTION).status_code == 200
        state = client.get('/api/state').json()
        assert state['strategy_settings']['evidence'] == 'on' and state['verification']['evidence'] == 'on'


def test_the_request_sent_to_the_ai_carries_the_evidence_instructions_and_the_pack(tmp_path, monkeypatch):
    import httpx
    from app.agents import Agents
    from app.store import Store
    config = Config(database_url='sqlite:///'+str(tmp_path/'ev.db'), mode='toss', password=PASSWORD, session_secret=SECRET,
                    toss_id='id', toss_secret='secret', gemini_key='', bridge_url='http://bridge.test:8765', bridge_token='t'*40,
                    providers='claude')
    store = Store(config.database_url, config.mode)
    with store.edit() as state:
        state.update(running=True, generation=1)
    sent = []

    def post(url, **kwargs):
        sent.append(kwargs['json'])
        return httpx.Response(200, json={'ok': False, 'message': 'stop here'}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx, 'post', post)
    ctx = {'strategy_mode': 'intraday', 'strategy_settings': {'horizon': 'month'}, 'reports': [], 'candidates': []}
    for with_pack in (False, True):
        try:
            Agents(config, store).run('director', {**ctx, **({'evidence': evidence.for_ai(pack(), ['momentum'])} if with_pack else {})}, 1)
        except Exception:
            pass
    plain, carried = sent
    assert '과거 20년' not in plain['system'] and '과거 20년' in carried['system']
    assert '"triggered_rules"' in carried['prompt'] and 'triggered_rules' not in plain['prompt']
    store.release()


def test_a_pack_built_today_from_old_prices_is_refused(tmp_path):
    store = EvidenceStore(tmp_path)
    old = pack()
    old['as_of_bar'] = (datetime.now()-__import__('datetime').timedelta(days=12)).date().isoformat()
    write(tmp_path, old)
    assert store.get('005930') is None
    recent = pack()
    recent['as_of_bar'] = (datetime.now()-__import__('datetime').timedelta(days=3)).date().isoformat()   # a weekend
    write(tmp_path, recent)
    assert store.get('005930')['symbol'] == '005930'
    write(tmp_path, dict(recent, stale=True))
    assert store.get('005930') is None


def test_a_long_answer_is_shortened_not_rejected_and_a_bad_one_names_its_role_and_reason(tmp_path, monkeypatch):
    import httpx
    from app.agents import Agents, validate_report
    from app.store import Store
    ctx = {'strategy_mode': 'intraday', 'strategy_settings': {'horizon': 'month'}, 'reports': []}
    base = {'stance': 'HOLD', 'quantity': 0, 'summary': 's', 'target_weight_pct': 0, 'stop_loss_pct': 5, 'take_profit_pct': 10,
            'max_holding_minutes': 20160, 'tasks': []}
    long = dict(base, risks=['r']*40, evidence=[{'claim': 'c'*3000, 'source_url': None}]*20)
    report = validate_report(long, 'technical', ctx, [], desk=True)
    assert len(report['risks']) <= 31 and report['evidence'] == []
    config = Config(database_url='sqlite:///'+str(tmp_path/'v.db'), mode='toss', password=PASSWORD, session_secret=SECRET,
                    toss_id='id', toss_secret='secret', gemini_key='', bridge_url='http://bridge.test:8765', bridge_token='t'*40,
                    providers='claude')
    store = Store(config.database_url, config.mode)
    with store.edit() as s:
        s.update(running=True, generation=1)
    bad = dict(base, risks=[], evidence=[], stop_loss_pct=99)
    monkeypatch.setattr(httpx, 'post', lambda url, **k: httpx.Response(200, json={'ok': True, 'data': bad}, request=httpx.Request('POST', url)))
    with pytest.raises(Exception) as err:
        Agents(config, store).run('technical', ctx, 1)
    assert 'technical' in str(err.value) and 'stop_loss_pct' in str(err.value)
    store.release()
