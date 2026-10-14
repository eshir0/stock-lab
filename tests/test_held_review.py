"""Held names in a pool-scan experiment (strategy_settings.held_review): with 'add' a held name whose BUY rule fires again
is analysed for adding to it, and the AI's SELL there is not acted on; with 'off' (saved experiments) held names are left
to the stop, trailing stop and holding limit. 2026-10-08: a one-share trial buy of HD현대중공업 was never looked at again."""
import time

from app import agents, verification
from app.config import Config
from app.risk import normalize_settings
from test_month_desk import bars, desk, open_position, run_cycle, series  # noqa: F401  (fixture)


def pool(engine, review):
    with engine.store.edit() as s:
        s['strategy_settings'].update(scan='pool', held_review=review)
        s['signal_names'] = {'experiment_id': s['experiment_id'], 'time': time.time(), 'KR': [], 'US': []}


def test_saved_experiments_keep_held_names_out_and_new_ones_review_them():
    assert normalize_settings({})['held_review'] == 'off'
    assert normalize_settings({'held_review': 'add'})['held_review'] == 'add'


def test_off_leaves_a_held_name_to_the_exit_rules(desk):
    open_position(desk, '005930', 70000.0, quantity=1)
    pool(desk, 'off')
    desk.provider.daily['005930'] = bars(series(70000, 'up'))
    state = run_cycle(desk)
    check = next(c for c in state['desk_gate']['checked'] if c['symbol'] == '005930')
    assert check['reason'] == 'holding' and desk.calls == []


def test_add_reviews_a_held_name_whose_buy_rule_fires_again(desk):
    open_position(desk, '005930', 70000.0, quantity=1)
    pool(desk, 'add')
    desk.provider.daily['005930'] = bars(series(70000, 'up'))
    state = run_cycle(desk)
    run = state['runs'][-1]
    assert run['symbol'] == '005930' and 'director' in desk.calls
    director = next(c for r, c in desk.contexts if r == 'director')
    assert director['add_review'] is True and director['rule_triggers']['side'] == 'BUY'
    assert 0 < director['position_weight_pct'] < 30
    assert '추가 매수' in agents.with_evidence('x', 'director', director)
    record = state['evaluations'][-1]
    assert record.get('trigger_side', 'BUY') in ('BUY', None)


def test_add_mode_without_a_buy_signal_says_so_and_makes_no_call(desk):
    open_position(desk, '005930', 70000.0, quantity=1)
    pool(desk, 'add')
    state = run_cycle(desk)
    check = next(c for c in state['desk_gate']['checked'] if c['symbol'] == '005930')
    assert check['reason'] == 'held_quiet' and desk.calls == []
    assert '보유 중 · 매수 신호 없음' in state['scheduler_status']


def test_a_sell_verdict_in_an_add_review_is_not_acted_on(desk):
    open_position(desk, '005930', 70000.0, quantity=1)
    pool(desk, 'add')
    desk.provider.daily['005930'] = bars(series(70000, 'up'))
    spy = desk.agents.run
    def selling(role, context, generation):
        report = spy(role, context, generation)
        return dict(report, stance='SELL', target_weight_pct=0) if role == 'director' else report
    desk.agents.run = selling
    state = run_cycle(desk)
    assert state['positions']['005930']['quantity'] == 1                       # still held
    assert not [t for t in state['trades'] if t['side'] == 'SELL']
    assert any('매도 의견이 나왔지만' in e['message'] for e in state['events'])


def test_the_add_review_prompts_are_part_of_the_strategy(monkeypatch):
    cfg = Config(mode='demo', password='x'*8, session_secret='y'*32)
    before = verification.fingerprint(cfg)
    monkeypatch.setitem(agents.HELD_ADD_PROMPT, 'director', agents.HELD_ADD_PROMPT['director']+' x')
    assert verification.fingerprint(cfg) != before


def test_the_form_defaults_to_reviewing_held_names():
    import pathlib
    html = (pathlib.Path(__file__).resolve().parents[1]/'app/static/index.html').read_text(encoding='utf-8')
    select = html.split('id="held-review"')[1].split('</select>')[0]
    assert select.index('value="add"') < select.index('value="off"')


def test_a_conditional_entry_fill_starts_the_re_analysis_wait(desk):
    # 2026-10-09: AMD filled by a pullback plan at 02:23 was analysed again at 02:36 (AVGO 03:42 -> 03:57): the fill's record
    # carries no rule signals, so it never matched the rules firing now. The fill now counts as a look.
    open_position(desk, '005930', 70000.0, quantity=1)
    pool(desk, 'add')
    desk.provider.daily['005930'] = bars(series(70000, 'up'))
    every = {'golden_cross': 'BUY', 'momentum': 'BUY', 'mean_reversion': 'BUY', 'breakout': 'BUY'}
    now = time.time()
    with desk.store.edit() as s:
        s.setdefault('evaluations', [])
        s['evaluations'] += [
            {'run_id': 'r1', 'symbol': '005930', 'time': now-3600, 'price': 73000.0, 'rules': every, 'selected_by': 'server'},
            {'run_id': 'r1', 'symbol': '005930', 'time': now-600, 'price': 70000.0, 'rules': {}, 'selected_by': 'watch'}]
    state = run_cycle(desk)
    check = next(c for c in state['desk_gate']['checked'] if c['symbol'] == '005930')
    assert check['reason'] == 'recent' and desk.calls == []
    look = desk.last_look(state, '005930')
    assert look['rules'] == every and look['price'] == 70000.0 and abs(look['time']-(now-600)) < 1
