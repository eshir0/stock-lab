"""Conditional entries, unit level: the plan the director may leave with a HOLD, what the server checks before it keeps the plan,
the price test it applies on every poll, and how the AI's answer is validated. No ledger, no market data, no AI service."""
import httpx
import pytest

from app import entry
from app.agents import Agents, DESK_SCHEMA, DESK_PROMPTS, DIRECTOR_SCHEMA, MONTH_PROMPTS, desk_prompts, validate_report
from app.config import Config
from app.providers import ProviderError
from app.store import Store

NOW = 1_000_000.0
QUOTE = {'bid': 100.0, 'ask': 100.1, 'session_end': NOW+10_000_000}
NEUTRAL = {'entry_type': 'none', 'entry_level': 0, 'entry_invalidate': 0, 'entry_minutes': 0}


def decision(**over):
    base = {'stance': 'HOLD', 'target_weight_pct': 20, 'stop_loss_pct': 2, 'take_profit_pct': 4, 'max_holding_minutes': 60,
            'entry_type': 'breakout', 'entry_level': 101.0, 'entry_invalidate': 99.5, 'entry_minutes': 60}
    base.update(over)
    return base


def plan(quote=QUOTE, horizon='intraday', **over):
    return entry.plan_from(decision(**over), quote=quote, horizon=horizon, now=NOW, currency='USD')


# ---- the plan the server keeps ------------------------------------------------------------------------------------------

def test_a_well_formed_breakout_becomes_a_plan_with_the_numbers_to_trade_it():
    fields, note = plan()
    assert note == '' and fields['type'] == 'breakout' and fields['level'] == 101.0 and fields['invalidate'] == 99.5
    assert fields['expires'] == NOW+3600
    assert fields['plan'] == {'target_weight_pct': 20, 'stop_loss_pct': 2, 'take_profit_pct': 4, 'max_holding_minutes': 60}


def test_a_pullback_needs_a_level_below_the_bid():
    assert plan(entry_type='pullback', entry_level=99.5, entry_invalidate=98.8)[0]['type'] == 'pullback'
    fields, note = plan(entry_type='pullback', entry_level=100.0, entry_invalidate=99.0)
    assert fields is None and '눌림' in note


@pytest.mark.parametrize('over, word', [
    ({'entry_level': 100.1}, '이미 현재 호가 이하'),                                   # a breakout level at the ask is not a breakout
    ({'entry_level': 100.0, 'entry_invalidate': 99.0}, '이미 현재 호가 이하'),
    ({'entry_level': 109.0}, '떨어져'),                                                # 8.9% away, more than a day trade can expect
    ({'entry_level': 101.0, 'entry_invalidate': 85.0}, '너무 멀어'),                   # the plan would be alive 16% below its own level
    ({'entry_invalidate': 100.2}, '현재 호가 이상'),                                   # dead before it starts
    ({'take_profit_pct': 2.9}, '손익비'),
    ({'take_profit_pct': 4, 'stop_loss_pct': 0}, '손익비'),
    ({'target_weight_pct': 0}, '목표 비중'),
])
def test_a_plan_that_does_not_fit_the_market_or_the_risk_rules_is_declined_with_a_reason(over, word):
    fields, note = plan(**over)
    assert fields is None and word in note


def test_only_a_hold_with_a_plan_type_leaves_a_plan():
    assert plan(stance='BUY') == (None, '')
    assert plan(stance='SELL') == (None, '')
    assert plan(entry_type='none') == (None, '')


def test_how_long_a_plan_waits_is_bounded_and_never_runs_into_the_close():
    assert plan(entry_minutes=5000)[0]['expires'] == NOW+180*60
    assert plan(entry_minutes=1)[0]['expires'] == NOW+10*60
    near = dict(QUOTE, session_end=NOW+1800)
    assert plan(quote=near)[0]['expires'] == NOW+1800-entry.CLOSE_MARGIN                  # waits until 10 minutes before the bell
    fields, note = plan(quote=dict(QUOTE, session_end=NOW+900))
    assert fields is None and '장 마감' in note
    month = plan(quote=near, horizon='month', entry_minutes=600, stop_loss_pct=5, take_profit_pct=10, max_holding_minutes=20160)[0]
    assert month['expires'] == NOW+600*60                                                # a month plan may wait past the close


def test_a_month_plan_may_sit_further_from_the_price_than_a_day_trade():
    assert plan(entry_level=110.0)[0] is None
    assert plan(horizon='month', entry_level=110.0, stop_loss_pct=5, take_profit_pct=10, max_holding_minutes=20160)[0] is not None


def test_a_bad_moment_gets_a_few_retries_not_one_and_not_forever():
    assert 3 <= entry.MAX_FAILURES <= 12                       # ten seconds apart: a minute or so of patience
    assert 1 <= entry.MAX_WAITING <= 8 and entry.CONFIRM_POLLS >= 2


def test_make_watch_carries_the_plan_and_starts_waiting():
    fields, _ = plan()
    watch = entry.make_watch(fields, symbol='AAPL', name='Apple', market='US', currency='USD', horizon='intraday', reference=100.05,
                             summary='x'*900, engine='Claude · model', run_id='r1', generation=3, now=NOW)
    assert watch['status'] == 'waiting' and watch['hits'] == 0 and watch['closed'] is None and watch['level'] == 101.0
    assert len(watch['summary']) == 600 and watch['generation'] == 3 and watch['run_id'] == 'r1' and watch['plan']['stop_loss_pct'] == 2


# ---- the price test on every poll ---------------------------------------------------------------------------------------

def watch(kind='breakout', level=101.0, dead=99.5):
    return {'type': kind, 'level': level, 'invalidate': dead}


def test_a_breakout_buys_between_its_level_and_half_a_percent_above_it():
    w = watch()
    assert entry.evaluate(w, {'bid': 100.5, 'ask': 100.9}) == 'wait'
    assert entry.evaluate(w, {'bid': 100.9, 'ask': 101.0}) == 'hit'
    assert entry.evaluate(w, {'bid': 101.4, 'ask': 101.5}) == 'hit'
    assert entry.evaluate(w, {'bid': 101.6, 'ask': 101.6}) == 'above'        # it ran past the zone: no chasing
    assert entry.evaluate(w, {'bid': 99.5, 'ask': 99.6}) == 'invalid'


def test_a_pullback_buys_once_the_ask_is_at_or_below_its_level():
    w = watch('pullback', 99.0, 98.0)
    assert entry.evaluate(w, {'bid': 99.4, 'ask': 99.6}) == 'wait'
    assert entry.evaluate(w, {'bid': 98.9, 'ask': 99.0}) == 'hit'
    assert entry.evaluate(w, {'bid': 98.1, 'ask': 98.3}) == 'hit'
    assert entry.evaluate(w, {'bid': 98.0, 'ask': 98.2}) == 'invalid'       # the bid reached the price where the idea is dead


def test_invalidation_beats_a_hit():
    assert entry.evaluate(watch('pullback', 99.0, 98.0), {'bid': 97.0, 'ask': 97.2}) == 'invalid'


def candles(recent, prior, before=20):
    return [{'volume': prior} for _ in range(before)]+[{'volume': recent} for _ in range(5)]


def test_breakout_volume_compares_the_last_five_minutes_with_the_twenty_before():
    assert entry.volume_ratio(candles(200, 100)) == 2.0
    assert entry.volume_ok(candles(100, 100)) is True                        # exactly the average is enough
    assert entry.volume_ok(candles(99, 100)) is False
    assert entry.volume_ratio(candles(1, 1)[-4:]) is None                    # too few candles to tell
    assert entry.volume_ratio(candles(5, 0)) is None                         # no volume before: nothing to compare with
    assert entry.volume_ok([]) is False
    assert entry.volume_ratio([{'volume': 'x'}]*30) is None


# ---- the ledger ---------------------------------------------------------------------------------------------------------

def test_close_waiting_can_be_limited_to_one_symbol():
    s = {'watches': [{'symbol': 'A', 'status': 'waiting'}, {'symbol': 'B', 'status': 'waiting'}, {'symbol': 'A', 'status': 'filled'}]}
    assert entry.close_waiting(s, 'replaced', 'x', 5.0, 'A') == 1
    assert [w['status'] for w in s['watches']] == ['replaced', 'waiting', 'filled'] and s['watches'][0]['closed'] == 5.0
    assert entry.close_waiting(s, 'cancelled', 'y', 6.0) == 1 and s['watches'][1]['outcome'] == 'y'
    assert entry.close_waiting({}, 'cancelled', 'y', 6.0) == 0


def test_trim_keeps_every_waiting_watch_and_only_the_newest_finished_ones():
    s = {'watches': [{'id': i, 'status': 'expired'} for i in range(entry.KEEP+5)]+[{'id': 'w', 'status': 'waiting'}]}
    entry.trim(s)
    ids = [w['id'] for w in s['watches']]
    assert 'w' in ids and len(ids) == entry.KEEP+1 and 0 not in ids and entry.KEEP+4 in ids
    entry.trim(s)
    assert len(s['watches']) == entry.KEEP+1


def test_waiting_and_find():
    s = {'watches': [{'id': 'a', 'status': 'waiting'}, {'id': 'b', 'status': 'filled'}]}
    assert [w['id'] for w in entry.waiting(s)] == ['a'] and entry.find(s, 'b')['status'] == 'filled' and entry.find(s, 'zz') is None
    assert entry.waiting({}) == [] and entry.find({}, 'a') is None


def test_a_plan_reads_back_in_plain_words():
    w = {'type': 'breakout', 'level': 70700.0, 'invalidate': 69300.0, 'currency': 'KRW'}
    assert entry.describe(w) == '돌파 매수 · 70,700원 이상이 되면 (무효 69,300원 이하)'
    p = {'type': 'pullback', 'level': 153.2, 'invalidate': 150.1, 'currency': 'USD'}
    assert entry.describe(p) == '눌림 매수 · $153.20 이하로 내려오면 (무효 $150.10 이하)'


# ---- the director's answer ----------------------------------------------------------------------------------------------

def test_clean_fields_keeps_a_good_plan_and_drops_a_bad_one_without_raising():
    good = {'entry_type': 'pullback', 'entry_level': 99, 'entry_invalidate': 98, 'entry_minutes': 30}
    assert entry.clean_fields(good) == ({'entry_type': 'pullback', 'entry_level': 99.0, 'entry_invalidate': 98.0, 'entry_minutes': 30}, '')
    assert entry.clean_fields({}) == (NEUTRAL, '')
    assert entry.clean_fields({'entry_type': 'none', 'entry_level': 5}) == (NEUTRAL, '')
    for bad in ({**good, 'entry_type': 'limit'}, {**good, 'entry_invalidate': 99}, {**good, 'entry_invalidate': 0},
                {**good, 'entry_minutes': 0}, {**good, 'entry_minutes': 30.5}, {**good, 'entry_level': 'x'},
                {**good, 'entry_level': True}, {**good, 'entry_level': float('nan')}, {**good, 'entry_minutes': True},
                {**good, 'entry_minutes': 10**9}):
        fields, note = entry.clean_fields(bad)
        assert fields == NEUTRAL and '올바르지 않아' in note


def director(**over):
    report = {'summary': 's', 'stance': 'HOLD', 'quantity': 0, 'risks': [], 'target_weight_pct': 20, 'stop_loss_pct': 2,
              'take_profit_pct': 4, 'max_holding_minutes': 60, 'tasks': [], 'evidence': [], 'entry_type': 'breakout',
              'entry_level': 101.0, 'entry_invalidate': 99.5, 'entry_minutes': 60}
    report.update(over)
    return report


def judged(report, role='director', horizon='intraday', grounded=True):
    context = {'strategy_settings': {'horizon': horizon, 'max_position_pct': 100},
               'reports': [{'role': 'news', 'evidence': [{'claim': 'c'}], 'sources': []}] if grounded else []}
    return validate_report(report, role, context, [], desk=True)


def test_a_good_plan_passes_the_validator_untouched():
    out = judged(director())
    assert (out['entry_type'], out['entry_level'], out['entry_invalidate'], out['entry_minutes']) == ('breakout', 101.0, 99.5, 60)
    assert out['stance'] == 'HOLD' and out['risks'] == []


def test_a_malformed_plan_costs_the_plan_not_the_analysis():
    out = judged(director(entry_invalidate=102.0))                      # the dead price above the level
    assert out['stance'] == 'HOLD' and out['summary'] == 's' and {k: out[k] for k in entry.FIELDS} == NEUTRAL
    assert any('올바르지 않아' in r for r in out['risks'])
    assert judged(director(entry_type='limit'))['entry_type'] == 'none'


def test_the_fields_may_be_missing_altogether_as_in_older_answers():
    report = director()
    for key in entry.FIELDS:
        report.pop(key)
    assert {k: judged(report)[k] for k in entry.FIELDS} == NEUTRAL


@pytest.mark.parametrize('over', [{'stance': 'BUY', 'quantity': 1}, {'stance': 'SELL'}, {'target_weight_pct': 0}])
def test_a_plan_only_survives_on_a_hold_that_has_a_weight(over):
    out = judged(director(**over))
    assert {k: out[k] for k in entry.FIELDS} == NEUTRAL and out['risks'] == []       # dropped quietly: the decision itself acts


def test_a_hold_forced_by_missing_evidence_leaves_no_plan_either():
    out = judged(director(), grounded=False)
    assert out['stance'] == 'HOLD' and {k: out[k] for k in entry.FIELDS} == NEUTRAL


def test_the_other_roles_cannot_leave_a_plan():
    out = judged(director(), role='critic')
    assert not any(key in out for key in entry.FIELDS)


def test_the_month_horizon_validates_the_same_fields():
    out = judged(director(stop_loss_pct=5, take_profit_pct=10, max_holding_minutes=20160, entry_minutes=600), horizon='month')
    assert out['entry_minutes'] == 600 and out['entry_type'] == 'breakout'


def test_only_the_director_is_asked_for_a_plan_and_every_field_is_required():
    assert not set(entry.FIELDS) & set(DESK_SCHEMA['properties'])
    assert set(entry.FIELDS) <= set(DIRECTOR_SCHEMA['properties']) and set(DIRECTOR_SCHEMA['required']) == set(DIRECTOR_SCHEMA['properties'])
    assert DIRECTOR_SCHEMA['properties']['entry_type']['enum'] == ['none', 'breakout', 'pullback']
    assert DIRECTOR_SCHEMA['additionalProperties'] is False and DIRECTOR_SCHEMA['properties']['entry_minutes'] == {'type': 'integer'}
    assert set(DESK_SCHEMA['properties']) < set(DIRECTOR_SCHEMA['properties'])


def test_the_prompts_explain_the_plan_for_each_horizon_and_the_memo_for_both():
    for prompts, span in ((DESK_PROMPTS, '10~180'), (MONTH_PROMPTS, '30~1440')):
        assert 'entry_type' in prompts['director'] and span in prompts['director'] and 'context.previous' in prompts['director']
        assert 'context.previous' in prompts['planner']
    assert desk_prompts('month')['director'] is MONTH_PROMPTS['director']
    assert '10~180' not in MONTH_PROMPTS['director']
    assert all('entry_type' not in DESK_PROMPTS[role] for role in ('planner', 'fundamental', 'technical', 'news', 'critic', 'selector'))


# ---- what actually goes over the wire -------------------------------------------------------------------------------------

@pytest.fixture
def bridge_agent(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'wire.db'), mode='toss', password='test-password-123456',
                    session_secret='test-session-secret-1234567890123456', toss_id='test-toss-client', toss_secret='test-toss-secret',
                    gemini_key='', bridge_url='http://bridge.test:8765', bridge_token='t'*40, providers='claude')
    store = Store(config.database_url, config.mode)
    with store.edit() as state:
        state.update(running=True, generation=1)
    yield Agents(config, store)
    store.release()


def ask(agent, monkeypatch, role, horizon='intraday'):
    sent = []

    def post(url, **kwargs):
        sent.append(kwargs['json'])
        return httpx.Response(200, json={'ok': True, 'data': director(**({'stance': 'HOLD'} if horizon == 'intraday' else {
            'stop_loss_pct': 5, 'take_profit_pct': 10, 'max_holding_minutes': 20160, 'entry_minutes': 600})), 'sources': [], 'model': 'm'},
                              request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx, 'post', post)
    context = {'strategy_mode': 'intraday', 'strategy_settings': {'horizon': horizon, 'max_position_pct': 100},
               'reports': [{'role': 'news', 'evidence': [{'claim': 'c'}], 'sources': []}]}
    try:
        result = agent.run(role, context, 1)
    except ProviderError:
        result = None                                          # a planner needs three tasks; only the request matters here
    return sent[0], result


def test_only_the_directors_request_carries_the_plan_schema_and_prompt(bridge_agent, monkeypatch):
    request, result = ask(bridge_agent, monkeypatch, 'director')
    assert set(entry.FIELDS) <= set(request['schema']['properties']) and 'entry_type' in request['system']
    assert result['entry_type'] == 'breakout' and result['entry_minutes'] == 60 and result['engine'] == 'Claude · m'
    for role in ('critic', 'planner', 'technical'):
        request, result = ask(bridge_agent, monkeypatch, role)
        assert not set(entry.FIELDS) & set(request['schema']['properties']) and 'entry_type' not in request['system']
        assert result is None or not any(key in result for key in entry.FIELDS)


def test_the_month_director_is_asked_for_a_month_sized_plan(bridge_agent, monkeypatch):
    request, result = ask(bridge_agent, monkeypatch, 'director', horizon='month')
    assert '30~1440' in request['system'] and '10~180' not in request['system'] and result['entry_minutes'] == 600
