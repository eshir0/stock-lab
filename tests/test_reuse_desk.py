"""Research reuse on a throw-away ledger: the second look at a name soon after the first one calls only the tape analyst, the critic and
the director (3 AI calls instead of 6), and every reason to look afresh is honoured. Synthetic quotes and scripted answers; no market
data or AI service is contacted."""
import time
from pathlib import Path

import pytest

from app import reuse
from app.agents import DESK_PROMPTS, MONTH_PROMPTS
from app.config import Config
from app.instruments import SYMBOLS
from app.providers import ProviderError
from test_entry_desk import MONTH, PRICE, at, build

STATIC = Path(__file__).resolve().parents[1]/'app'/'static'
JS = (STATIC/'app.js').read_text(encoding='utf-8')
FRESH = ['planner', 'fundamental', 'technical', 'news', 'critic', 'director']
REUSED = ['technical', 'critic', 'director']


def sourced(engine):
    """Give the scripted company and news reports the sourced evidence a real answer would carry (a reuse needs something to build on)."""
    inner = engine.agents.run

    def run(role, context, generation):
        report = inner(role, context, generation)
        if role in ('fundamental', 'news'):
            report.update(evidence=[{'claim': 'c', 'source_url': 'https://example.com/'+role, 'published_at': None}],
                          sources=[{'url': 'https://example.com/'+role, 'title': role}])
        return report
    engine.agents.run = run


def quiet(engine):
    """The director only ever holds, so the ledger stays empty and the tests are about the research alone."""
    engine.script = {'stance': 'HOLD', 'quantity': 0, 'target_weight_pct': 0}
    return engine


def cycle_on(engine, symbol='005930'):
    """An ordinary analysis of `symbol` (the cycle picks it by cursor and market), not one the owner asked for."""
    engine.provider.closed = {'000660' if symbol == '005930' else '005930'}            # one Korean candidate: no selector call
    engine.refresh()
    with engine.store.edit() as s:
        s['cursor'] = [i['symbol'] for i in engine.active_instruments(s)].index(symbol)
        s['last_market'] = 'US' if SYMBOLS[symbol]['market'] == 'KR' else 'KR'
        s['next_run'] = 0
    engine.calls.clear()
    engine.contexts.clear()
    engine.cycle()
    return engine.store.read()


@pytest.fixture
def day(tmp_path):
    engine, store = build(tmp_path)
    sourced(quiet(engine))
    yield engine
    store.release()


@pytest.fixture
def month(tmp_path):
    engine, store = build(tmp_path, MONTH)
    sourced(quiet(engine))
    yield engine
    store.release()


def roles(state, index=-1):
    return [r['role'] for r in state['runs'][index]['reports']]


# ---- the second look ----------------------------------------------------------------------------------------------------

def test_the_second_look_reuses_the_research_and_makes_only_three_calls(day):
    first = cycle_on(day)
    assert sorted(day.calls) == sorted(FRESH) and 'reuse' not in first['runs'][-1] and 'reused' not in first['evaluations'][-1]
    originals = {r['role']: r for r in first['runs'][-1]['reports']}
    second = cycle_on(day)
    assert day.calls == REUSED                                                      # in this order: tape, objections, decision
    run = second['runs'][-1]
    assert run['status'] == 'completed' and run['reuse'] == {'from_run': first['runs'][-1]['id'], 'age_minutes': 0, 'saved_calls': 3}
    by_role = {r['role']: r for r in run['reports']}
    assert roles(second) == ['planner', 'fundamental', 'news', 'technical', 'critic', 'director']        # each exactly once
    assert roles(first) == ['planner', 'fundamental', 'technical', 'news', 'critic', 'director']
    for role in ('planner', 'fundamental', 'news'):
        assert by_role[role]['reused'] is True and by_role[role]['age_minutes'] == 0
        assert by_role[role]['time'] == originals[role]['time'] and by_role[role]['evidence'] == originals[role]['evidence']
        assert by_role[role]['research_price'] == PRICE and by_role[role]['usage'] == {}
    for role in ('technical', 'critic', 'director'):
        assert 'reused' not in by_role[role] and by_role[role]['time'] > originals['news']['time']
    assert second['evaluations'][-1]['reused'] is True and 'reused' not in second['evaluations'][-2]
    assert any('조사 재사용' in e['message'] and '3회 절약' in e['message'] for e in second['events'])
    assert run['price'] == PRICE and first['runs'][-1]['price'] == PRICE


def test_what_each_role_is_told_when_the_research_is_reused(day):
    cycle_on(day)
    cycle_on(day)
    contexts = dict(day.contexts)
    technical = contexts['technical']
    assert technical['assignment']['role'] == 'technical' and technical['assignment']['instruction']
    assert [r['role'] for r in technical['reports']] == ['planner']                 # it sees what a parallel analyst would see
    for role in ('critic', 'director'):
        reports = contexts[role]['reports']
        assert [r['role'] for r in reports][:4] == ['planner', 'fundamental', 'technical', 'news']
        assert [bool(r.get('reused')) for r in reports][:4] == [True, True, False, True]
        assert all(r['age_minutes'] == 0 for r in reports if r.get('reused'))
    assert 'reused' in DESK_PROMPTS['critic'] and 'reused' in DESK_PROMPTS['director']
    assert 'reused' in MONTH_PROMPTS['critic'] and 'reused' in MONTH_PROMPTS['director']
    assert all('reused' not in DESK_PROMPTS[role] for role in ('planner', 'fundamental', 'technical', 'news', 'selector'))


def test_the_team_panel_shows_the_reused_roles_as_done_not_busy(day):
    cycle_on(day)
    cycle_on(day)
    run = day.store.read()['runs'][-1]
    assert run['active_role'] is None and run['active_roles'] == []                 # finished: nothing marked as running
    # while the tape analyst works only that role is marked busy (the saved state at that moment)
    seen = []
    inner = day.agents.run

    def watch(role, context, generation):
        if role == 'technical':
            state = day.store.read()['runs'][-1]
            seen.append((state['active_role'], state['active_roles'], [r['role'] for r in state['reports']], day.store.read()['scheduler_status']))
        return inner(role, context, generation)
    day.agents.run = watch
    cycle_on(day)
    active_role, active_roles, saved, status = seen[0]
    assert active_role == 'research' and active_roles == ['technical'] and {'planner', 'fundamental', 'news'} <= set(saved)
    assert '조사 재사용' in status


# ---- every reason to look afresh -------------------------------------------------------------------------------------------

def test_a_price_move_means_new_research_and_the_log_says_why(day):
    cycle_on(day)
    at(day, PRICE*1.02)
    state = cycle_on(day)
    assert sorted(day.calls) == sorted(FRESH) and 'reuse' not in state['runs'][-1] and 'reused' not in state['evaluations'][-1]
    assert any('움직여 조사를 새로 합니다' in e['message'] for e in state['events'])
    assert state['runs'][-1]['price'] == pytest.approx(PRICE*1.02)                  # the new research is anchored at the new price


def test_a_volume_surge_means_new_research(day):
    cycle_on(day)
    day.provider.recent_volume = 1_000_000                                          # ten times the minute volume before
    state = cycle_on(day)
    assert sorted(day.calls) == sorted(FRESH)
    assert any('거래량이 평소의' in e['message'] and '조사를 새로 합니다' in e['message'] for e in state['events'])


def test_research_that_is_too_old_is_not_reused(day):
    cycle_on(day)
    with day.store.edit() as s:
        for report in s['runs'][-1]['reports']:
            report['time'] -= day.c.research_reuse_seconds+60
    state = cycle_on(day)
    assert sorted(day.calls) == sorted(FRESH) and 'reuse' not in state['runs'][-1]


def test_an_analysis_the_owner_asked_for_always_investigates_afresh(day):
    cycle_on(day)
    day.calls.clear()
    day.request_cycle('005930')
    day.cycle()
    state = day.store.read()
    assert sorted(day.calls) == sorted(FRESH) and 'reuse' not in state['runs'][-1] and state['runs'][-1]['selected_by'] == 'user'


def test_research_without_sourced_evidence_is_not_reused(tmp_path):
    engine, store = build(tmp_path)                                                 # the scripted answers carry no evidence
    quiet(engine)
    cycle_on(engine)
    state = cycle_on(engine)
    assert sorted(engine.calls) == sorted(FRESH) and 'reuse' not in state['runs'][-1]
    store.release()


def test_another_name_does_not_borrow_the_research(day):
    cycle_on(day, '005930')
    state = cycle_on(day, '000660')
    assert state['runs'][-1]['symbol'] == '000660' and sorted(day.calls) == sorted(FRESH)


def test_a_reuse_can_be_switched_off(tmp_path):
    engine, store = build(tmp_path, research_reuse_seconds=0)
    sourced(quiet(engine))
    cycle_on(engine)
    state = cycle_on(engine)
    assert sorted(engine.calls) == sorted(FRESH) and 'reuse' not in state['runs'][-1]
    assert Config().research_reuse_seconds == 3600 and Config().research_reuse_move_pct == 1.5
    store.release()


def test_a_failed_tape_analysis_leaves_a_clean_error_and_no_decision(day, monkeypatch):
    cycle_on(day)
    inner = day.agents.run

    def broken(role, context, generation):
        if role == 'technical':
            raise ProviderError('[Claude 사용량 소진] 시험')
        return inner(role, context, generation)
    monkeypatch.setattr(day.agents, 'run', broken)
    state = cycle_on(day)
    run = state['runs'][-1]
    assert run['status'] == 'error' and '소진' in run['error'] and 'director' not in [r['role'] for r in run['reports']]
    assert state['trades'] == [] and len(state['evaluations']) == 1                 # no second decision was recorded


def test_a_surprise_in_old_data_just_means_a_full_analysis(day, monkeypatch):
    cycle_on(day)

    def odd(*args, **kwargs):
        raise TypeError('an old run in a shape nobody expected')
    monkeypatch.setattr(reuse, 'find', odd)
    state = cycle_on(day)
    assert sorted(day.calls) == sorted(FRESH) and state['runs'][-1]['status'] == 'completed' and 'reuse' not in state['runs'][-1]


# ---- chains, horizons, the dashboard ---------------------------------------------------------------------------------------

def test_a_chain_of_reuses_never_extends_the_research_beyond_its_original_age_or_price(day):
    first = cycle_on(day)
    at(day, PRICE*1.01)
    second = cycle_on(day)                                                          # +1.0%: reused
    assert day.calls == REUSED and second['runs'][-1]['reports'][1]['research_price'] == PRICE
    at(day, PRICE*1.0186)                                                           # +1.86% from the research, only +0.85% from the last run
    third = cycle_on(day)
    assert sorted(day.calls) == sorted(FRESH) and 'reuse' not in third['runs'][-1]
    assert first['runs'][-1]['reports'][0]['time'] == second['runs'][-1]['reports'][0]['time']          # the reused copy kept the original time


def test_the_month_horizon_reuses_research_too(month):
    first = cycle_on(month)
    assert sorted(month.calls) == sorted(FRESH) and first['runs'][-1]['horizon'] == 'month'
    second = cycle_on(month)
    assert month.calls == REUSED and second['runs'][-1]['reuse']['saved_calls'] == 3 and second['evaluations'][-1]['horizon'] == 'month'


def test_the_dashboard_marks_reused_research():
    for text in ("done.reused ? `재사용 · ${done.age_minutes}분 전`", "run.reuse ? ` · 조사 ${run.reuse.age_minutes}분 전 자료 재사용(AI ${run.reuse.saved_calls}회 절약)`",
                 "r.reused ? ' · 재사용 ' + r.age_minutes + '분 전'"):
        assert text in JS


def test_the_run_remembers_the_price_it_started_at(day):
    state = cycle_on(day)
    assert state['runs'][-1]['price'] == PRICE
    assert time.time()-state['runs'][-1]['time'] < 120
