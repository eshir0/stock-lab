"""Research reuse, unit level: when the earlier planner, company and news research may be used again for a second look at the same
name, and when it must be looked up afresh. Plain dicts only; no ledger, no market data, no AI service."""
import copy

import pytest

from app import reuse

NOW = 1_000_000.0


def report(role, age=600, evidence=True, **extra):
    return {'role': role, 'time': NOW-age, 'summary': role, 'sources': [], 'usage': {'total_tokens': 5}, 'search_entry_point': '<b>x</b>',
            'evidence': [{'claim': 'c', 'source_url': 'https://example.com/'+role}] if evidence else [], **extra}


def planner(age=600, roles=('fundamental', 'technical', 'news'), tasks=None, **extra):
    return report('planner', age, tasks=[{'role': role, 'instruction': 'look'} for role in roles] if tasks is None else tasks, **extra)


def run(symbol='A', status='completed', horizon='intraday', price=100.0, reports=None, run_id='r1', **extra):
    reports = [planner(), report('fundamental'), report('technical'), report('news')] if reports is None else reports
    return {'id': run_id, 'symbol': symbol, 'status': status, 'horizon': horizon, 'price': price, 'time': NOW-700, 'reports': reports, **extra}


def look(*runs, **over):
    args = dict(price=100.5, spike=None, now=NOW, ttl=3600, move_pct=1.5, horizon='intraday')
    args.update(over)
    return reuse.find({'runs': list(runs)}, 'A', **args)


def test_a_recent_sourced_research_at_about_the_same_price_is_reused():
    found, note = look(run())
    assert note == '' and found['run_id'] == 'r1' and found['age_minutes'] == 10 and found['price'] == 100.0
    assert set(found['reports']) == {'planner', 'fundamental', 'news'}                      # the tape is never reused
    for role, copy_ in found['reports'].items():
        assert copy_['reused'] is True and copy_['age_minutes'] == 10 and copy_['research_price'] == 100.0
        assert copy_['role'] == role and copy_['time'] == NOW-600                          # the original time stays on it
        assert copy_['usage'] == {} and 'search_entry_point' not in copy_ and copy_['evidence']
    assert found['reports']['planner']['tasks'][0]['role'] == 'fundamental'


def test_the_stored_reports_are_never_touched_by_a_reuse():
    state = {'runs': [run()]}
    before = copy.deepcopy(state)
    found, _ = reuse.find(state, 'A', price=100.5, spike=None, now=NOW, ttl=3600, move_pct=1.5, horizon='intraday')
    found['reports']['planner']['tasks'].clear()
    found['reports']['news']['evidence'].clear()
    assert state == before


def test_zero_seconds_turns_reuse_off():
    assert look(run(), ttl=0) == (None, '')
    just_now = run(reports=[planner(0), report('fundamental', 0), report('news', 0)])          # research made this very second
    assert look(just_now, ttl=3600)[0] is not None and look(just_now, ttl=0) == (None, '')


@pytest.mark.parametrize('runs', [
    [], [run(symbol='B')], [run(status='running')], [run(status='error')], [run(horizon='month')],
    [run(), run(reports=[planner(), report('fundamental'), report('technical')])],                   # the LATEST completed run decides
])
def test_only_the_latest_completed_analysis_of_this_name_and_horizon_can_be_reused(runs):
    assert look(*runs) == (None, '')


def test_a_run_without_a_horizon_is_a_day_trading_run():
    old = run()
    del old['horizon']
    assert look(old)[0] is not None and look(old, horizon='month') == (None, '')


@pytest.mark.parametrize('missing', ['planner', 'fundamental', 'news'])
def test_a_run_that_lacks_any_of_the_three_researches_is_not_reused(missing):
    reports = [r for r in (planner(), report('fundamental'), report('technical'), report('news')) if r['role'] != missing]
    assert look(run(reports=reports)) == (None, '')


def test_the_planner_must_have_handed_out_all_three_tasks():
    assert look(run(reports=[planner(roles=('fundamental', 'news')), report('fundamental'), report('news')])) == (None, '')
    assert look(run(reports=[planner(tasks='x'), report('fundamental'), report('news')])) == (None, '')


def test_research_older_than_the_limit_is_not_reused_and_the_oldest_report_decides():
    assert look(run(reports=[planner(3600), report('fundamental', 3600), report('news', 3600)]))[0] is not None      # the limit itself is fine
    assert look(run(reports=[planner(3601), report('fundamental'), report('news')])) == (None, '')
    assert look(run(reports=[planner(), report('fundamental'), report('news', 4000)])) == (None, '')
    assert look(run(), ttl=500) == (None, '')


def test_research_with_no_sourced_evidence_is_not_built_upon():
    assert look(run(reports=[planner(), report('fundamental', evidence=False), report('news', evidence=False)])) == (None, '')
    assert look(run(reports=[planner(), report('fundamental', evidence=False), report('news')]))[0] is not None
    assert look(run(reports=[planner(), report('fundamental'), report('news', evidence=False)]))[0] is not None


def test_a_report_without_a_time_is_not_reused():
    bad = report('news')
    bad['time'] = None
    assert look(run(reports=[planner(), report('fundamental'), bad])) == (None, '')
    bad['time'] = 'yesterday'
    assert look(run(reports=[planner(), report('fundamental'), bad])) == (None, '')


def test_a_price_move_of_the_limit_or_more_means_new_research_and_says_so():
    found, note = look(run(), price=101.6)
    assert found is None and '1.6%' in note and '조사를 새로 합니다' in note
    assert look(run(), price=98.0)[0] is None and '2.0%' in look(run(), price=98.0)[1]                    # down as well as up
    assert look(run(), price=101.4)[0] is not None and look(run(), price=98.6)[0] is not None
    assert look(run(), price=100.0, move_pct=.5)[0] is not None and look(run(), price=100.6, move_pct=.5)[0] is None


@pytest.mark.parametrize('anchor', [None, 0, -5, True, 'x'])
def test_research_made_at_an_unknown_price_cannot_be_judged_and_is_not_reused(anchor):
    assert look(run(price=anchor)) == (None, '')


@pytest.mark.parametrize('price', [None, 0, -1, 'x'])
def test_an_unknown_current_price_is_not_reused_on(price):
    assert look(run(), price=price) == (None, '')


def test_a_volume_surge_means_new_research_and_says_so():
    found, note = look(run(), spike=3.0)
    assert found is None and '3.0배' in note and '조사를 새로 합니다' in note
    assert look(run(), spike=2.99)[0] is not None and look(run(), spike=None)[0] is not None and look(run(), spike=0.4)[0] is not None


def test_the_price_check_comes_before_the_volume_check():
    assert '움직여' in look(run(), price=110.0, spike=9.0)[1]


def test_a_reuse_keeps_the_original_price_and_time_so_a_chain_of_reuses_cannot_keep_stale_research_alive():
    first = run(run_id='r1', price=100.0)
    found, _ = look(first)
    second = run(run_id='r2', price=101.0, reports=[found['reports']['planner'], found['reports']['fundamental'], report('technical', 60),
                                                     found['reports']['news']])
    again, _ = look(first, second, price=101.2)
    assert again['run_id'] == 'r2' and again['price'] == 100.0 and again['age_minutes'] == 10
    drifted, note = look(first, second, price=101.6)                              # +1.6% from the ORIGINAL price, only +0.6% from the last run
    assert drifted is None and '1.6%' in note
    stale = run(run_id='r3', price=130.0, reports=[dict(r, time=NOW-3700) for r in second['reports']])
    assert look(first, second, stale, price=130.5, ttl=3600) == (None, '')       # a recent run built from research that is now too old
