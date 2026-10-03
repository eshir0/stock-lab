"""The daily focus list: data screen, AI merge, held-position rotation and outcome scoring (no I/O, no network)."""
import time

import pytest

from app import universe as u
from app.agents import Agents, validate_trend
from app.config import Config
from app.instruments import SYMBOLS

DAY = 86400
START = 1_700_000_000


def candles(closes, volume=1e9, spread=.01, completed=True, start=START):
    return [{'time': start+i*DAY, 'open': c, 'high': c*(1+spread), 'low': c*(1-spread), 'close': c, 'volume': volume,
             'currency': 'USD', 'interval': '1d', 'completed': completed} for i, c in enumerate(closes)]


def trend(n=40, daily=.004, base=100.0):
    return [base*(1+daily)**i for i in range(n)]


def item(symbol='AAPL', **extra):
    return {**SYMBOLS.get(symbol, {'symbol': symbol, 'name': symbol, 'currency': 'USD', 'market': 'US'}), **extra}


NOW = START+40*DAY


# ---- measurement -------------------------------------------------------------------------------------------

def test_metrics_follow_simple_formulas():
    closes = trend()
    m = u.daily_metrics(candles(closes), now=NOW)
    assert m['last'] == pytest.approx(closes[-1], rel=1e-4)
    assert m['ret_1m_pct'] == pytest.approx((closes[-1]/closes[-22]-1)*100, abs=.01)
    assert m['ret_5d_pct'] == pytest.approx((closes[-1]/closes[-6]-1)*100, abs=.01)
    assert m['ext_20d_pct'] == pytest.approx((closes[-1]/(sum(closes[-20:])/20)-1)*100, abs=.01)
    assert m['up_ratio'] == 1.0 and m['sessions'] == 40
    assert 1.5 < m['atr_pct'] < 2.6              # ~2% high-low range plus the small daily drift


def test_a_day_still_in_progress_never_enters_the_measurement():
    closes = trend()
    rows = candles(closes)
    rows.append({**rows[-1], 'time': rows[-1]['time']+DAY, 'close': closes[-1]*.5, 'completed': False})
    assert u.daily_metrics(rows, now=NOW) == u.daily_metrics(candles(closes), now=NOW)


def test_too_little_stale_or_broken_data_gives_no_metrics():
    assert u.daily_metrics(candles(trend(20)), now=START+20*DAY) is None                 # under 26 sessions
    assert u.daily_metrics(candles(trend()), now=NOW+30*DAY) is None                     # newest candle is a month old
    assert u.daily_metrics([], now=NOW) is None and u.daily_metrics(None, now=NOW) is None
    bad = candles(trend())
    bad[10]['close'] = float('nan')
    assert u.daily_metrics(bad, now=NOW)['sessions'] == 39                               # the bad row is skipped, not trusted
    duplicated = candles(trend()) + candles(trend())[-3:]
    assert u.daily_metrics(duplicated, now=NOW)['sessions'] == 40


def test_missing_high_and_low_fall_back_to_close_changes():
    rows = candles(trend())
    for r in rows:
        r.pop('high'), r.pop('low')
    assert u.daily_metrics(rows, now=NOW)['atr_pct'] == pytest.approx(.4, abs=.05)


# ---- the screen: each caution is a hard rule ----------------------------------------------------------------

def reasons(closes, symbol='AAPL', **kw):
    return u.screen(u.daily_metrics(candles(closes, **kw), now=NOW), item(symbol))


def test_a_steady_uptrend_passes():
    assert reasons(trend()) == []


def test_downtrend_names_are_excluded_because_the_desk_only_buys():
    assert any('20일선 아래' in r for r in reasons(trend(daily=-.004)))


def test_a_name_far_above_its_20_day_average_is_not_chased():
    closes = trend(40, .022)                      # accelerating climb: ~+21% over its 20-day average, only ~+11% in 5 days
    m = u.daily_metrics(candles(closes), now=NOW)
    assert m['ext_20d_pct'] > 12 and m['ret_5d_pct'] < 18
    found = u.screen(m, item())
    assert any('20일선보다' in r and '추격 금지' in r for r in found) and not any('5일' in r for r in found)


def test_a_five_day_spike_is_not_chased():
    closes = trend(35, .002) + [100*1.002**35*(1+.05*k) for k in range(1, 6)]  # +25% in five sessions
    assert any('5일' in r and '급등' in r for r in reasons(closes))


def test_too_quiet_or_too_wild_names_are_excluded():
    assert any('변동폭' in r for r in reasons(trend(), spread=.0005))
    assert any('변동폭' in r for r in reasons(trend(), spread=.09))


def test_thinly_traded_names_are_excluded_but_relaxed_mode_skips_only_that_floor():
    thin = u.daily_metrics(candles(trend(), volume=1000), now=NOW)
    assert 'turnover' in ''.join(u.screen(thin, item())) or any('거래대금' in r for r in u.screen(thin, item()))
    assert u.screen(thin, item(), relaxed=True) == []


def test_a_data_glitch_jump_in_the_candles_is_flagged():
    closes = trend()
    closes[-8] = closes[-9]*1.6
    assert any('급변' in r for r in reasons(closes))


def test_leveraged_etfs_get_looser_but_still_bounded_caps():
    wild = trend(40, .004)
    wild[-1] = wild[-2]*1.02
    m = u.daily_metrics(candles(wild, spread=.05), now=NOW)          # ~10% ATR: too wild for a stock, fine for 3x
    assert any('변동폭' in r for r in u.screen(m, item('AAPL')))
    assert not any('변동폭' in r for r in u.screen(m, item('TQQQ')))
    assert u.kind_of(item('TQQQ')) == 'lev' and u.kind_of(item('QQQ')) == 'etf' and u.kind_of(item('AAPL')) == 'stock'


# ---- scoring and ranking ---------------------------------------------------------------------------------------

def metrics_for(daily, **kw):
    return u.daily_metrics(candles(trend(daily=daily), **kw), now=NOW)


def test_stronger_trend_scores_higher_and_scores_stay_in_range():
    weak, strong = metrics_for(.0015), metrics_for(.005)
    a, _ = u.score(weak, item()), None
    b = u.score(strong, item())
    assert 0 <= a[0] < b[0] <= 100
    assert set(b[1]) == {'trend', 'consistency', 'tradability', 'liquidity', 'attention', 'extension_penalty'}


def test_stretched_names_lose_points():
    calm = metrics_for(.004)
    stretched = dict(calm, ext_20d_pct=11.5)
    assert u.score(stretched, item())[0] < u.score(calm, item())[0]


def test_market_attention_is_a_bonus_but_missing_rankings_are_neutral():
    m = metrics_for(.004)
    top = u.score(m, item(), {'AAPL': {'amount': 3, 'volume': 40}})[0]
    outside = u.score(m, item(), {'MSFT': {'amount': 3}})[0]
    neutral = u.score(m, item(), None)[0]
    assert outside < neutral < top


def test_rank_pool_sorts_by_score_and_explains_every_exclusion():
    pool = [item('AAPL'), item('MSFT'), item('NVDA'), item('AMD')]
    metrics = {'AAPL': metrics_for(.002), 'MSFT': metrics_for(.005), 'NVDA': metrics_for(-.004), 'AMD': None}
    passed, excluded = u.rank_pool(pool, metrics)
    assert [p['symbol'] for p in passed] == ['MSFT', 'AAPL']
    assert passed[0]['score'] > passed[1]['score']
    why = {e['symbol']: e['reasons'] for e in excluded}
    assert set(why) == {'NVDA', 'AMD'} and why['AMD'] == ['일봉 데이터 부족'] and why['NVDA']


# ---- merging the AI read: it can re-order and veto, never add ---------------------------------------------------

def ranked(*symbols):
    return [{'symbol': s, 'name': s, 'score': 90-i*5, 'kind': 'stock'} for i, s in enumerate(symbols)]


def brief(picks=(), avoid=(), grounded=True):
    return {'grounded': grounded, 'picks': [{'symbol': s, 'theme': 't', 'catalyst': 'c', 'priced_in_risk': r, 'reason': 'x'} for s, r in picks],
            'avoid': [{'symbol': s, 'reason': 'bad news'} for s in avoid]}


def test_without_an_ai_read_the_list_is_purely_the_data_ranking():
    picks, notes = u.choose(ranked('A', 'B', 'C', 'D'), None, 3)
    assert [p['symbol'] for p in picks] == ['A', 'B', 'C'] and {p['source'] for p in picks} == {'data'} and notes == []


def test_the_ai_can_reorder_low_risk_before_medium_and_fill_comes_from_data():
    picks, _ = u.choose(ranked('A', 'B', 'C', 'D'), brief([('C', 'medium'), ('D', 'low')]), 3)
    assert [(p['symbol'], p['source']) for p in picks] == [('D', 'ai'), ('C', 'ai'), ('A', 'data')]
    assert picks[0]['ai']['priced_in_risk'] == 'low'


def test_an_invented_symbol_can_never_enter_the_list():
    picks, _ = u.choose(ranked('A', 'B'), brief([('ZZZZ', 'low'), ('B', 'low')]), 3)
    assert 'ZZZZ' not in [p['symbol'] for p in picks] and [p['symbol'] for p in picks] == ['B', 'A']


def test_a_name_the_ai_calls_priced_in_is_dropped_and_not_filled_back_in():
    picks, notes = u.choose(ranked('A', 'B', 'C'), brief([('A', 'high'), ('B', 'low')]), 3)
    assert [p['symbol'] for p in picks] == ['B', 'C']
    assert any('A' in n and '반영' in n for n in notes)


def test_the_avoid_list_removes_names_from_the_fill_too():
    picks, _ = u.choose(ranked('A', 'B', 'C'), brief([('C', 'low')], avoid=['A']), 3)
    assert [p['symbol'] for p in picks] == ['C', 'B']


def test_a_read_without_cited_sources_is_ignored_entirely():
    picks, notes = u.choose(ranked('A', 'B', 'C'), brief([('C', 'low')], avoid=['A'], grounded=False), 2)
    assert [p['symbol'] for p in picks] == ['A', 'B'] and {p['source'] for p in picks} == {'data'}
    assert notes and '출처' in notes[0]


def test_the_list_never_exceeds_n_and_never_repeats():
    picks, _ = u.choose(ranked('A', 'B', 'C', 'D'), brief([('A', 'low'), ('A', 'low'), ('B', 'low'), ('C', 'low'), ('D', 'low')]), 2)
    assert [p['symbol'] for p in picks] == ['A', 'B']
    assert u.choose([], brief([('A', 'low')]), 3)[0] == []


def test_the_ai_only_sees_screened_names_and_server_numbers():
    shown = u.ai_candidates(ranked(*'ABCDEFGHIJ'), limit=4)
    assert [c['symbol'] for c in shown] == list('ABCD') and set(shown[0]) >= {'symbol', 'score', 'ret_1m_pct', 'ext_20d_pct'}


# ---- the AI reply is validated on the server ------------------------------------------------------------------------

CTX = {'candidates': [{'symbol': 'AAPL'}, {'symbol': 'MSFT'}]}
SOURCES = [{'url': 'https://news.example/a', 'title': 'A'}]


def raw(**over):
    base = {'market_view': '위험 선호가 살아 있습니다.', 'themes': ['AI 반도체'],
            'picks': [{'symbol': 'AAPL', 'theme': 't', 'catalyst': '신제품', 'priced_in_risk': 'low', 'reason': 'r'}],
            'avoid': [], 'evidence': [{'claim': '신제품 발표', 'source_url': 'https://news.example/a', 'published_at': None}], 'risks': []}
    base.update(over)
    return base


def test_a_read_that_cites_a_retrieved_url_is_grounded():
    out = validate_trend(raw(), CTX, SOURCES)
    assert out['grounded'] and out['picks'][0]['symbol'] == 'AAPL' and out['stance'] == 'HOLD' and out['quantity'] == 0


def test_evidence_that_was_not_actually_retrieved_does_not_ground_the_read():
    out = validate_trend(raw(evidence=[{'claim': '지어낸 근거', 'source_url': 'https://made-up.example/x', 'published_at': None}]), CTX, SOURCES)
    assert not out['grounded'] and out['evidence'] == []


def test_symbols_outside_the_offered_candidates_are_ignored_and_reported():
    pick = {'symbol': 'FAKE', 'theme': 't', 'catalyst': 'c', 'priced_in_risk': 'low', 'reason': 'r'}
    out = validate_trend(raw(picks=[pick, raw()['picks'][0]], avoid=[{'symbol': 'ALSO-FAKE', 'reason': 'x'}]), CTX, SOURCES)
    assert [p['symbol'] for p in out['picks']] == ['AAPL'] and out['avoid'] == [] and out['ignored'] == 2
    assert any('무시' in r for r in out['risks'])


@pytest.mark.parametrize('bad', [
    {'market_view': ''}, {'themes': 'x'}, {'picks': 'x'}, {'evidence': 'x'}, {'risks': [1]},
    {'picks': [{'symbol': 'AAPL', 'theme': 't', 'catalyst': 'c', 'priced_in_risk': 'maybe', 'reason': 'r'}]},
    {'picks': [{'symbol': 'AAPL'}]}, {'avoid': [{'symbol': 1, 'reason': 'x'}]}])
def test_malformed_replies_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_trend(raw(**bad), CTX, SOURCES)
    with pytest.raises(ValueError):
        validate_trend('not an object', CTX, SOURCES)


def test_the_demo_provider_returns_a_scripted_read_and_no_network_is_used(tmp_path):
    config = Config(database_url='sqlite:///'+str(tmp_path/'a.db'), mode='demo', password='test-password-123456',
                    session_secret='test-secret-123456789012345678901234', toss_id='', toss_secret='', gemini_key='')
    reply = Agents(config, None).run('trend', {'strategy_mode': 'intraday', 'candidates': [{'symbol': 'AAPL'}, {'symbol': 'MSFT'}], 'max_picks': 1}, 1)
    assert reply['engine'] == '시험 응답' and [p['symbol'] for p in reply['picks']] == ['AAPL'] and reply['grounded']


# ---- what happens to a name that is already held ---------------------------------------------------------------------

POSITION = {'quantity': 3, 'average': 100.0}


def metrics(**over):
    return {'last': 110.0, 'sma20': 105.0, 'ret_5d_pct': 2.0, **over}


@pytest.mark.parametrize('price,trend_ok,action', [
    (110.0, True, 'keep'),       # winner, trend intact: let it run under the stop / target / time rules
    (95.0, True, 'keep'),        # loser, trend intact: do not lock the loss because the list changed
    (110.0, False, 'sell'),      # winner, trend broken: lock the gain
    (95.0, False, 'sell')])      # loser, trend broken: cut it
def test_rotation_keeps_healthy_names_and_sells_broken_ones(price, trend_ok, action):
    m = metrics() if trend_ok else metrics(last=100.0, sma20=104.0)
    decision = u.rotation_decision(POSITION, m, price)
    assert decision['action'] == action and decision['trend_ok'] is trend_ok and decision['reason']
    assert decision['pnl_pct'] == pytest.approx((price/100-1)*100, abs=.01)


def test_rotation_reasons_distinguish_locking_a_gain_from_cutting_a_loss():
    assert '수익' in u.rotation_decision(POSITION, metrics(last=100.0, sma20=104.0), 110.0)['reason']
    assert '손실' in u.rotation_decision(POSITION, metrics(last=100.0, sma20=104.0), 95.0)['reason']


def test_a_five_day_slide_or_an_ai_warning_breaks_the_trend():
    assert u.rotation_decision(POSITION, metrics(ret_5d_pct=-4.0), 105.0)['action'] == 'sell'
    warned = u.rotation_decision(POSITION, metrics(), 105.0, avoid=True)
    assert warned['action'] == 'sell' and 'AI' in warned['reason']


def test_without_daily_data_or_price_the_existing_exit_rules_stay_in_charge():
    assert u.rotation_decision(POSITION, None, 105.0)['action'] == 'keep'
    assert u.rotation_decision(POSITION, metrics(), None)['pnl_pct'] is None


# ---- measuring whether the picks were any good ------------------------------------------------------------------------

def series_after(ref_close, next_close, symbol='X'):
    return {symbol: [{'time': START, 'close': ref_close, 'completed': True}, {'time': START+DAY, 'close': next_close, 'completed': True}]}


def test_outcome_compares_the_picks_with_the_whole_pool_and_the_fixed_lineup():
    record = {'ref': {s: [START, 100.0] for s in 'ABCD'}, 'picks': ['A', 'B'], 'fixed': ['C', 'D']}
    series = {s: [{'time': START, 'close': 100.0, 'completed': True}, {'time': START+DAY, 'close': c, 'completed': True}]
              for s, c in zip('ABCD', (104.0, 102.0, 98.0, 100.0))}
    got = u.outcome(record, series)
    assert got['pick_pct'] == 3.0 and got['pool_pct'] == 1.0 and got['fixed_pct'] == -1.0
    assert got['excess_pool_pct'] == 2.0 and got['excess_fixed_pct'] == 4.0 and got['n_picks'] == 2 and got['n_pool'] == 4


def test_outcome_waits_until_the_next_completed_candle_exists():
    record = {'ref': {'X': [START, 100.0]}, 'picks': ['X'], 'fixed': []}
    assert u.outcome(record, {'X': [{'time': START, 'close': 100.0, 'completed': True}]}) is None
    unfinished = {'X': [{'time': START, 'close': 100.0, 'completed': True}, {'time': START+DAY, 'close': 120.0, 'completed': False}]}
    assert u.outcome(record, unfinished) is None


def test_summary_shows_the_sample_size_and_never_invents_numbers():
    assert u.summarize([]) == {'all': {'days': 0}, 'KR': {'days': 0}, 'US': {'days': 0}}
    history = [{'market': 'US', 'result': {'pick_pct': 1.0, 'pool_pct': .5, 'excess_pool_pct': .5, 'excess_fixed_pct': None}},
               {'market': 'US', 'result': {'pick_pct': -1.0, 'pool_pct': 0.0, 'excess_pool_pct': -1.0, 'excess_fixed_pct': -.5}},
               {'market': 'KR', 'result': None}]
    got = u.summarize(history)
    assert got['US']['days'] == 2 and got['US']['excess_pool_pct'] == -.25 and got['US']['beat_pool_days'] == 1
    assert got['US']['excess_fixed_pct'] == -.5 and got['KR'] == {'days': 0} and got['all']['days'] == 2


def test_a_name_both_recommended_and_avoided_is_avoided():
    from app.universe import choose
    passed = [{'symbol': 'SPY'}, {'symbol': 'QQQ'}, {'symbol': 'AAPL'}]
    ai = {'grounded': True, 'picks': [{'symbol': 'SPY', 'priced_in_risk': 'low'}, {'symbol': 'QQQ', 'priced_in_risk': 'low'}],
          'avoid': [{'symbol': 'SPY'}]}
    picks, notes = choose(passed, ai, 2)
    assert [p['symbol'] for p in picks] == ['QQQ', 'AAPL'] and any('회피로 처리' in n for n in notes)
