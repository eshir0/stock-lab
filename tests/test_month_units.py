"""The month horizon's building blocks: settings and sizing, the trailing stop, plan validation, three months of history,
the rule-signal gate and trading-day scoring. Pure functions; no market data or AI service is contacted."""
import math
import time

import pytest

from app import evaluation, gate, history
from app.agents import Agents, MONTH_PROMPTS, DESK_PROMPTS, desk_prompts, validate_report
from app.config import Config
from app.risk import BOUNDS, MONTH_MINUTES, RiskError, horizon_of, normalize_settings, size_order, trailed_stop

DAY = 86400
NOW = 1_800_000_000


# ---- settings and sizing ------------------------------------------------------------------------------------------------

def test_a_saved_experiment_without_a_horizon_keeps_the_same_session_rules():
    assert normalize_settings({})['horizon'] == 'intraday' and normalize_settings({})['max_holding_minutes'] == 120
    assert horizon_of({}) == 'intraday' and horizon_of(None) == 'intraday' and horizon_of({'horizon': 'nonsense'}) == 'intraday'


def test_month_settings_default_to_thirty_days_and_reject_intraday_numbers():
    assert normalize_settings({'horizon': 'month'})['max_holding_minutes'] == MONTH_MINUTES == 43200
    assert normalize_settings({'horizon': 'month', 'max_holding_minutes': 1440})['max_holding_minutes'] == 1440
    for bad in (120, 1439, 43201):
        with pytest.raises(RiskError):
            normalize_settings({'horizon': 'month', 'max_holding_minutes': bad})
    with pytest.raises(RiskError):
        normalize_settings({'horizon': 'year'})
    with pytest.raises(RiskError):
        normalize_settings({'horizon': 'intraday', 'max_holding_minutes': 43200})


QUOTE = {'bid': 99.9, 'ask': 100.0, 'session_end': NOW+3600}
CONSTRAINTS = {'portfolio_equity': 1_000_000, 'max_buy_quantity': 5000, 'max_sell_quantity': 0}


class Cfg:
    slippage_bps, fee_kr, fee_us, sell_tax_kr = 5, 15, 15, 0


def plan(**over):
    decision = {'stance': 'BUY', 'target_weight_pct': 20, 'stop_loss_pct': 5, 'take_profit_pct': 12, 'max_holding_minutes': 20160}
    decision.update(over)
    return decision


def sized(settings, **over):
    state = {'strategy_settings': settings, 'positions': {}, 'quotes': {}}
    return size_order(state, '005930', QUOTE, plan(**over), CONSTRAINTS, Cfg, now=NOW)


def test_a_month_plan_is_held_overnight_and_not_cut_at_the_session_end():
    result = sized({'horizon': 'month'})
    assert result['quantity'] > 0 and result['expires_at'] == NOW+20160*60      # 14 days, far beyond the session end
    capped = sized({'horizon': 'intraday'}, stop_loss_pct=2, take_profit_pct=4, max_holding_minutes=240)
    assert capped['expires_at'] == QUOTE['session_end']-120                      # the older mode still closes before the bell


def test_the_planned_holding_never_exceeds_the_experiments_limit():
    assert sized({'horizon': 'month', 'max_holding_minutes': 4320})['expires_at'] == NOW+4320*60


@pytest.mark.parametrize('over', [{'stop_loss_pct': 1.9}, {'stop_loss_pct': 15.1, 'take_profit_pct': 30},
                                  {'max_holding_minutes': 1439}, {'max_holding_minutes': 43201}, {'take_profit_pct': 7}])
def test_month_plans_outside_the_bounds_are_refused(over):
    with pytest.raises(RiskError):
        sized({'horizon': 'month'}, **over)


def test_intraday_plans_keep_their_old_bounds():
    for over in ({'stop_loss_pct': 2, 'take_profit_pct': 4, 'max_holding_minutes': 60},):
        assert sized({'horizon': 'intraday'}, **over)['quantity'] > 0
    with pytest.raises(RiskError):
        sized({'horizon': 'intraday'}, stop_loss_pct=12, take_profit_pct=20, max_holding_minutes=60)


# ---- trailing stop ------------------------------------------------------------------------------------------------------

def held(**over):
    position = {'average': 100.0, 'stop_price': 95.0, 'take_profit_price': 110.0, 'trail_pct': 5.0}
    position.update(over)
    return position


def test_the_stop_stays_put_until_the_price_is_half_way_to_the_target():
    assert trailed_stop(held(), 104.9) == 95.0                                  # arms at 100 + (110-100)*.5 = 105
    assert trailed_stop(held(), 100.0) == 95.0


def test_once_armed_the_stop_locks_in_the_entry_and_then_follows_the_high():
    first = trailed_stop(held(), 105.0)
    assert first == pytest.approx(100.3, abs=.01) and first > 95.0             # entry plus a margin for costs
    assert trailed_stop(held(), 108.0) == pytest.approx(108*.95, abs=.01)       # follows the high by the planned distance


def test_the_stop_only_ever_rises_and_never_reaches_the_target():
    assert trailed_stop(held(stop_price=104.0), 105.0) == 104.0                 # already higher: unchanged
    assert trailed_stop(held(trail_pct=.1), 111.0) < 110.0                      # a tight trail cannot pass the target price
    stops = []
    stop = 95.0
    for high in (100, 105, 107, 106, 109, 104, 103):
        stop = trailed_stop(held(stop_price=stop), max(high, 100))
        stops.append(stop)
    assert stops == sorted(stops)


@pytest.mark.parametrize('missing', ['average', 'stop_price', 'take_profit_price', 'trail_pct'])
def test_a_position_without_a_plan_is_left_alone(missing):
    position = held()
    stop = position['stop_price']
    position.pop(missing)
    assert trailed_stop(position, 200.0) == position.get('stop_price')
    assert trailed_stop(dict(held(), trail_pct=float('nan')), 200.0) == stop


# ---- plan validation and prompts ----------------------------------------------------------------------------------------

def director(**over):
    report = {'summary': 's', 'stance': 'BUY', 'quantity': 1, 'risks': [], 'target_weight_pct': 10, 'stop_loss_pct': 5,
              'take_profit_pct': 12, 'max_holding_minutes': 20160, 'tasks': [], 'evidence': []}
    report.update(over)
    return report


def check(report, horizon, role='critic'):
    context = {'strategy_settings': {'horizon': horizon}, 'reports': []}
    return validate_report(report, role, context, [], desk=True)


def test_the_analysts_placeholder_numbers_are_valid_for_their_horizon():
    for horizon in ('month', 'intraday'):
        fallback = BOUNDS[horizon]['placeholder']
        assert check(director(stance='HOLD', quantity=0, target_weight_pct=0, **fallback), horizon)['stance'] == 'HOLD'


@pytest.mark.parametrize('over', [{'stop_loss_pct': 1}, {'stop_loss_pct': 16, 'take_profit_pct': 40}])
def test_month_validation_rejects_intraday_style_numbers(over):
    with pytest.raises(ValueError):
        check(director(**over), 'month')


def test_a_holding_period_out_of_range_is_brought_into_it():
    """Since 2026-10-07 a holding period outside the horizon's range is clamped, not refused: the server sets the real
    limit, and refusing failed a whole analysis when both AIs wrote the 90-day US hold."""
    assert check(director(max_holding_minutes=60), 'month')['max_holding_minutes'] == 1440
    assert check(director(max_holding_minutes=50000), 'month')['max_holding_minutes'] == 43200
    ok = director(stop_loss_pct=2, take_profit_pct=4, max_holding_minutes=60)
    assert check(ok, 'intraday')['max_holding_minutes'] == 60
    assert check(director(stop_loss_pct=2, take_profit_pct=4), 'intraday')['max_holding_minutes'] == BOUNDS['intraday']['holding'][1]


def test_month_prompts_replace_the_role_texts_but_keep_the_same_roles():
    assert set(MONTH_PROMPTS) <= set(DESK_PROMPTS)
    month = desk_prompts('month')
    assert desk_prompts('intraday') is DESK_PROMPTS and set(month) == set(DESK_PROMPTS)
    assert '43200' in month['director'] and '1개월' in month['director'] and '추적 손절' in month['director']
    assert 'daily_history' in month['technical'] and '3개월' in month['news']
    assert '43200' not in DESK_PROMPTS['director']


def test_demo_answers_follow_the_horizon_and_pass_validation():
    agents = Agents(Config(mode='demo', providers='', gemini_key='', bridge_url='', bridge_token=''), None)
    for horizon in ('month', 'intraday'):
        context = {'strategy_mode': 'intraday', 'strategy_settings': {'horizon': horizon}, 'position': {}, 'reports': [],
                   'constraints': {'max_buy_quantity': 10}}
        report = agents.run('director', context, 1)
        fallback = BOUNDS[horizon]['placeholder']
        assert (report['stop_loss_pct'], report['take_profit_pct'], report['max_holding_minutes']) == (
            fallback['stop_loss_pct'], fallback['take_profit_pct'], fallback['max_holding_minutes'])
        validate_report({k: v for k, v in report.items() if k not in ('usage', 'engine', 'sources')}, 'critic', context, [], desk=True)


# ---- three months of history --------------------------------------------------------------------------------------------

def bars(closes, end=NOW, spread=.005, volume=1_000):
    n = len(closes)
    return [{'time': end-(n-i)*DAY, 'open': c, 'high': c*(1+spread), 'low': c*(1-spread), 'close': c, 'volume': volume+i,
             'completed': True} for i, c in enumerate(closes)]


def test_only_completed_valid_recent_bars_are_kept_oldest_first():
    rows = bars([100+i for i in range(100)])
    rows[10]['completed'] = False
    rows[11]['close'] = float('nan')
    rows[12]['volume'] = -1
    rows.append(dict(rows[-1]))                                                  # duplicate timestamp
    rows.append('junk')
    kept = history.completed_bars(rows, NOW)
    times = [b['time'] for b in kept]
    assert times == sorted(times) and len(times) == len(set(times))
    assert times[0] >= NOW-history.HISTORY_DAYS*DAY and len(kept) <= history.HISTORY_DAYS
    assert all(b['completed'] for b in kept)


def test_the_summary_computes_the_numbers_the_analysts_would_otherwise_guess():
    closes = [100+i for i in range(70)]                                          # +1 a day
    s = history.summary(bars(closes))
    assert s['sessions'] == 70 and s['last_close'] == 169
    assert s['ret_1w_pct'] == pytest.approx((169/164-1)*100, abs=.01)
    assert s['ret_1m_pct'] == pytest.approx((169/148-1)*100, abs=.01)
    assert s['ret_3m_pct'] == pytest.approx(69.0, abs=.01)
    assert s['sma20'] == pytest.approx(sum(closes[-20:])/20) and s['sma60'] == pytest.approx(sum(closes[-60:])/60)
    assert s['high_date'] == s['to'] and s['from_low_pct'] > 60 and s['max_drawdown_pct'] == 0
    assert s['up_day_ratio_20d'] == 1.0 and s['atr_pct'] is not None and s['volume_ratio_5d'] is not None


def test_short_windows_report_none_instead_of_inventing_numbers():
    s = history.summary(bars([100+i for i in range(10)]))
    assert s['sessions'] == 10 and s['ret_1m_pct'] is None and s['ret_3m_pct'] is None and s['sma60'] is None and s['atr_pct'] is None
    assert history.summary([]) == {'sessions': 0} and history.block([]) is None


def test_the_block_is_a_compact_table_the_model_can_read():
    block = history.block(bars([100+i for i in range(30)]))
    assert block['columns'] == list(history.COLUMNS) and len(block['rows']) == 30
    assert block['rows'][0][0] < block['rows'][-1][0] and len(block['rows'][0]) == len(block['columns'])
    assert '진행 중인 오늘 봉 제외' in block['note']


def test_a_bar_stamped_at_local_or_utc_midnight_gets_the_right_date():
    kst_midnight = 1790607600                     # 2026-09-29 00:00 KST (the Korean daily bar stamp)
    assert history.day_label(kst_midnight) == '2026-09-29'
    assert history.day_label(kst_midnight+9*3600) == '2026-09-29'      # the same date stamped at UTC midnight


def test_own_records_show_only_this_symbols_recent_calls_and_fills():
    state = {'evaluations': [
        {'symbol': 'AAA', 'time': NOW-2*DAY, 'stance': 'BUY', 'price': 100, 'action': 'filled',
         'outcomes': {'d1': {'returns': {'AAA': 1.5, 'BBB': 9}}, 'd5': {'missed': True}}},
        {'symbol': 'BBB', 'time': NOW-DAY, 'stance': 'HOLD', 'price': 50, 'action': 'hold', 'outcomes': {}},
        {'symbol': 'AAA', 'time': NOW-200*DAY, 'stance': 'BUY', 'price': 90, 'action': 'filled', 'outcomes': {}}],
        'trades': [{'symbol': 'AAA', 'time': NOW-2*DAY, 'side': 'BUY', 'quantity': 3, 'price': 100, 'realized': 0},
                   {'symbol': 'BBB', 'time': NOW-DAY, 'side': 'BUY', 'quantity': 1, 'price': 50, 'realized': 0}]}
    own = history.own_records(state, 'AAA', NOW)
    assert [d['price'] for d in own['decisions']] == [100] and own['decisions'][0]['return_after_pct'] == {'d1': 1.5}
    assert [t['quantity'] for t in own['trades']] == [3]


def test_the_minute_block_keeps_the_latest_rows_in_a_compact_form():
    minutes = [{'time': NOW+60*i, 'open': 100+i, 'high': 101+i, 'low': 99+i, 'close': 100.5+i, 'volume': 10+i} for i in range(50)]
    block = history.minute_block(minutes)
    assert len(block['rows']) == 30 and block['rows'][-1][4] == 149.5 and block['columns'][0] == 'time'
    assert history.minute_block([]) is None and history.minute_block([{'close': float('nan'), 'time': 1}]) is None
    assert len(json_size(block)) < 3000


def json_size(value):
    import json
    return json.dumps(value)


# ---- the rule-signal gate -----------------------------------------------------------------------------------------------

def test_a_name_that_is_not_held_needs_a_buy_signal_and_a_held_name_a_sell_signal():
    signals = {'golden_cross': 'SELL', 'momentum': 'HOLD', 'mean_reversion': None, 'breakout': 'BUY'}
    assert gate.assess(held=False, signals=signals, last=None, price=100, now=NOW) == {
        'eligible': True, 'reason': 'signal', 'rules': ['breakout']}
    assert gate.assess(held=True, signals=signals, last=None, price=100, now=NOW)['rules'] == ['golden_cross']
    quiet = {'golden_cross': 'HOLD', 'momentum': 'HOLD', 'mean_reversion': None, 'breakout': 'HOLD'}
    assert gate.assess(held=False, signals=quiet, last=None, price=100, now=NOW) == {
        'eligible': False, 'reason': 'no_signal', 'rules': []}
    assert gate.assess(held=True, signals={'momentum': 'BUY'}, last=None, price=100, now=NOW)['eligible'] is False


def test_the_owner_can_always_ask_and_a_missing_reading_never_counts_as_a_signal():
    assert gate.assess(held=False, signals={}, last=None, price=None, now=NOW, forced=True)['eligible'] is True
    assert gate.assess(held=False, signals={'momentum': None, 'breakout': None}, last=None, price=None, now=NOW)['eligible'] is False


def test_the_same_name_is_not_analysed_again_unless_something_changed():
    last = {'time': NOW-3600, 'price': 100.0, 'rules': {'momentum': 'BUY'}}
    same = {'momentum': 'BUY'}
    assert gate.assess(held=False, signals=same, last=last, price=101, now=NOW)['reason'] == 'recent'
    assert gate.assess(held=False, signals=same, last=last, price=103.1, now=NOW)['eligible'] is True          # moved 3%
    assert gate.assess(held=False, signals=same, last=last, price=96.5, now=NOW)['eligible'] is True           # ... either way
    assert gate.assess(held=False, signals={'momentum': 'BUY', 'breakout': 'BUY'}, last=last, price=100, now=NOW)['eligible']
    old = dict(last, time=NOW-gate.REANALYZE_SECONDS-1)
    assert gate.assess(held=False, signals=same, last=old, price=100, now=NOW)['eligible'] is True             # long enough ago


def test_the_status_line_names_each_name_and_why():
    line = gate.summary_line([{'name': '삼성전자', 'reason': 'signal', 'rules': ['momentum', 'breakout']},
                              {'name': 'KODEX 200', 'reason': 'no_signal', 'rules': []},
                              {'name': 'SK하이닉스', 'reason': 'recent', 'rules': ['momentum']}])
    assert line == '삼성전자 규칙 신호(모멘텀·돌파) · KODEX 200 규칙 신호 없음 · SK하이닉스 최근 분석함(모멘텀)'


# ---- scoring on daily closes --------------------------------------------------------------------------------------------

def entry(**over):
    record = {'run_id': 'r', 'time': NOW, 'symbol': 'AAA', 'market': 'KR', 'horizon': 'month', 'stance': 'BUY',
              'price': 100.0, 'cost_bps': 20, 'candidates': {'BBB': 50.0}, 'rules': {'momentum': 'BUY'}, 'outcomes': {},
              'selected_by': 'ai', 'engine': 'Claude · x'}
    record.update(over)
    return record


def daily(closes, first_time):
    return [{'time': first_time+i*DAY, 'close': c, 'completed': True} for i, c in enumerate(closes)]


def test_the_close_n_trading_days_after_the_decision_day_is_what_counts():
    rows = daily([100, 101, 102, 103, 104, 105, 106], NOW-DAY//2)              # the decision day is index 0
    assert evaluation.day_return(rows, NOW, 1, 100.0) == 1.0
    assert evaluation.day_return(rows, NOW, 5, 100.0) == 5.0
    assert evaluation.day_return(rows, NOW, 21, 100.0) is None                  # that close does not exist yet
    assert evaluation.day_return([], NOW, 1, 100.0) is None and evaluation.day_return(rows, NOW, 1, 0) is None
    assert evaluation.day_return(rows, NOW-5*DAY, 1, 100.0) is None            # decided before the first bar we hold


def test_score_days_fills_due_horizons_only_and_keeps_the_others_pending():
    s = {'evaluations': [entry()]}
    rows = {'AAA': daily([100, 102, 103, 104, 105, 106], NOW-DAY//2), 'BBB': daily([50, 50.5, 51, 51, 52, 53], NOW-DAY//2)}
    assert evaluation.score_days(s, NOW+3*DAY, rows) == 1                      # only the one-day horizon is due yet
    out = s['evaluations'][0]['outcomes']
    assert set(out) == {'d1'} and out['d1']['returns'] == {'AAA': 2.0, 'BBB': 1.0}
    evaluation.score_days(s, NOW+8*DAY, rows)
    assert s['evaluations'][0]['outcomes']['d5']['returns']['AAA'] == 6.0 and 'd21' not in s['evaluations'][0]['outcomes']


def test_a_decision_whose_bars_never_arrive_is_marked_missed_eventually():
    s = {'evaluations': [entry()]}
    evaluation.score_days(s, NOW+60*DAY, {})
    assert all(s['evaluations'][0]['outcomes'][k] == {'time': NOW+60*DAY, 'missed': True} for k in ('d1', 'd5', 'd21'))


def test_symbols_due_lists_what_must_be_read_and_ignores_same_session_records():
    records = [entry(), entry(symbol='CCC', candidates={}, time=NOW+10), entry(symbol='DDD', horizon='intraday')]
    assert evaluation.symbols_due(records, NOW+3600) == set()
    assert evaluation.symbols_due(records, NOW+DAY+20) == {'AAA', 'BBB', 'CCC'}


def test_month_records_are_not_scored_at_thirty_or_sixty_minutes():
    s = {'quotes': {'AAA': {'bid': 101, 'ask': 101, 'asof': NOW+3600, 'received': NOW+3600, 'session_end': NOW+7200}},
         'evaluations': [entry()]}
    evaluation.update_outcomes(s, NOW+3600)
    assert s['evaluations'][0]['outcomes'] == {}


def test_the_summary_reports_day_horizons_for_month_records_and_flags_small_samples():
    scored = entry(outcomes={'d1': {'returns': {'AAA': 2.0, 'BBB': 1.0}}, 'd5': {'returns': {'AAA': 6.0}}})
    waiting = entry(run_id='w')
    summary = evaluation.summarize([scored, waiting])
    assert summary['active'] == ['d1', 'd5', 'd21'] and summary['labels']['d21'] == '21거래일 뒤(1달)'
    assert summary['pending'] == 2 and not summary['enough_sample']
    d5 = summary['horizons']['d5']
    assert d5['scored'] == 1 and d5['buy']['count'] == 1 and d5['buy']['avg_return_pct'] == 6.0
    assert d5['ai_avg_net_pct'] == pytest.approx(6.0-.2)
    assert d5['rules']['momentum']['trades'] == 1 and d5['rules']['momentum']['avg_net_pct'] == pytest.approx(6.0-.2)
    assert summary['horizons']['d1']['selector']['count'] == 1


def test_an_empty_or_intraday_only_summary_still_shows_the_old_horizons():
    assert evaluation.summarize([])['active'] == ['30', '60']
    old = entry(horizon='intraday', outcomes={'30': {'returns': {'AAA': 1.0}}})
    summary = evaluation.summarize([old])
    assert summary['active'] == ['30', '60'] and summary['pending'] == 1
