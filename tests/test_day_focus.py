"""Day-trading focus lists: names that really move within a day are preferred (in either direction), the morning brief
and the stock selector ask for the same thing, and a month plan keeps its rising-trend screen.
Synthetic candles and quotes on a throw-away ledger; no market data or AI service is contacted."""
import copy
import time

import pytest

from app import agents as agents_module
from app import universe as u
from app.agents import Agents, DESK_PROMPTS, TREND_VOLATILITY_PROMPT
from app.config import Config
from app.instruments import CATALOGUE
from app.providers import ProviderError
from test_focus_engine import DAY, build, make, open_now, picks, series
from test_universe import NOW, START, candles, item, trend

DAY_SETTINGS = {'horizon': 'intraday'}


def wide(closes=None, spread=.03, volume=1e9, **kw):
    return candles(closes or trend(), volume=volume, spread=spread, **kw)


# ---- what is measured ---------------------------------------------------------------------------------------------------

def test_the_average_daily_range_and_move_and_the_volume_surge_are_measured():
    rows = wide()                                                     # +0.4% a day, high/low 3% either side of the close
    m = u.daily_metrics(rows, now=NOW)
    assert m['range_pct'] == pytest.approx(6.0, abs=.01) and m['avg_move_pct'] == pytest.approx(.4, abs=.01)
    assert m['volume_ratio'] == 1.0
    rows[-1]['volume'] = 2e9
    assert u.daily_metrics(rows, now=NOW)['volume_ratio'] == 2.0


def test_without_high_and_low_the_range_falls_back_to_the_close_to_close_move():
    rows = wide()
    for r in rows:
        r.pop('high'), r.pop('low')
    assert u.daily_metrics(rows, now=NOW)['range_pct'] == pytest.approx(.4, abs=.05)


def test_a_zero_volume_history_gives_no_ratio_instead_of_dividing_by_zero():
    assert u.daily_metrics(wide(volume=0), now=NOW)['volume_ratio'] is None


# ---- the day-trading screen -----------------------------------------------------------------------------------------------

def why(rows, symbol='AAPL', profile='volatility', **kw):
    return u.screen(u.daily_metrics(rows, now=NOW), item(symbol, **kw), profile=profile)


def test_a_name_that_moves_enough_passes_without_any_trend_requirement():
    assert why(wide(spread=.03)) == []
    assert why(wide(trend(daily=-.004), spread=.03)) == []            # a downtrend below its 20-day line is fine for a day trader
    assert why(wide(trend(daily=.02), spread=.03)) == []              # ... and so is a stretched one: no chasing rule here


def test_the_same_downtrend_is_still_refused_on_the_month_screen():
    assert any('20일선 아래' in r for r in why(wide(trend(daily=-.004), spread=.03), profile='trend'))


def test_names_that_barely_move_are_left_out_because_costs_would_eat_the_gain():
    reasons = why(wide(spread=.005))
    assert len(reasons) == 1 and '단타 기준(1.8%)' in reasons[0]


def test_the_floor_depends_on_the_kind_of_instrument():
    rows = wide(spread=.007)                                           # a 1.4% range
    assert why(rows) and why(rows, 'QQQ') == []                        # too calm for a stock, enough for an ETF
    assert why(wide(spread=.007), 'TQQQ') != []                        # leveraged funds must move more (floor 2.0%)
    assert why(wide(spread=.011), 'TQQQ') == []


def test_a_name_that_is_too_wild_is_left_out():
    assert any('너무 큼' in r for r in why(wide(spread=.09)))          # an 18% average range


def test_a_five_day_freefall_is_left_out_because_the_desk_only_buys():
    closes = trend()[:-5] + [trend()[-6]*(1-.04*i) for i in range(1, 6)]      # about -19% over five days
    reasons = why(wide(closes, spread=.03))
    assert any('급락' in r for r in reasons)


def test_data_glitches_and_thin_trading_are_still_screened():
    glitch = trend()
    glitch[20] = glitch[19]*1.8
    assert any('급변' in r for r in why(wide(glitch)))
    assert 'avg' not in ''.join(why(wide(volume=1000)))               # not a typo guard: the floor reason is worded in Korean
    assert any('거래대금' in r for r in why(wide(volume=1000)))
    assert not any('거래대금' in r for r in u.screen(u.daily_metrics(wide(volume=1000), now=NOW), item(), relaxed=True, profile='volatility'))


# ---- scoring ------------------------------------------------------------------------------------------------------------------

def scored(rows, symbol='AAPL', attention=None, profile='volatility'):
    return u.score(u.daily_metrics(rows, now=NOW), item(symbol), attention, profile=profile)


def test_bigger_daily_moves_score_higher_and_the_parts_are_the_day_trading_ones():
    calm, lively, extreme = scored(wide(spread=.012)), scored(wide(spread=.02)), scored(wide(spread=.03))
    assert calm[0] < lively[0] < extreme[0] <= 100
    assert set(calm[1]) == {'volatility', 'liquidity', 'attention', 'activity'}
    assert extreme[1]['volatility'] == 100.0                           # a 6% range is past the "ideal" 5% mark for a stock


def test_yesterdays_volume_surge_is_a_bonus():
    busy = wide(spread=.03)
    busy[-1]['volume'] = 3e9
    assert scored(busy)[0] > scored(wide(spread=.03))[0]


def test_todays_biggest_movers_count_as_attention_on_the_day_trading_screen_only():
    rows = wide(spread=.03)
    mover = {'AAPL': {'gainers': 2}}
    assert scored(rows, attention=mover)[0] > scored(rows, attention={'MSFT': {'gainers': 2}})[0]
    assert scored(rows, attention=mover, profile='trend')[0] == scored(rows, attention={'MSFT': {'gainers': 2}}, profile='trend')[0]


def test_the_ranking_puts_the_most_volatile_liquid_names_first_and_explains_every_exclusion():
    pool = [item('AAPL'), item('MSFT'), item('NVDA'), item('AMD')]
    metrics = {'AAPL': u.daily_metrics(wide(spread=.02), now=NOW), 'MSFT': u.daily_metrics(wide(spread=.005), now=NOW),
               'NVDA': u.daily_metrics(wide(spread=.04), now=NOW), 'AMD': None}
    passed, excluded = u.rank_pool(pool, metrics, profile='volatility')
    assert [p['symbol'] for p in passed] == ['NVDA', 'AAPL']
    assert passed[0]['range_pct'] == pytest.approx(8.0, abs=.01) and 'volume_ratio' in passed[0] and 'avg_move_pct' in passed[0]
    assert {e['symbol']: e['reasons'][0] for e in excluded}['AMD'] == '일봉 데이터 부족'
    assert any('단타 기준' in r for e in excluded if e['symbol'] == 'MSFT' for r in e['reasons'])


def test_the_brief_is_shown_the_movement_numbers_and_the_mover_ranks():
    passed, _ = u.rank_pool([item('AAPL')], {'AAPL': u.daily_metrics(wide(), now=NOW)}, {'AAPL': {'amount': 4, 'gainers': 7}}, profile='volatility')
    shown = u.ai_candidates(passed)[0]
    assert shown['range_pct'] and shown['avg_move_pct'] is not None and shown['volume_ratio'] == 1.0
    assert shown['rank_amount'] == 4 and shown['rank_gainers'] == 7 and shown['rank_losers'] is None


# ---- judging a day-trading list: did the picks really move? ---------------------------------------------------------------------

def day_after(ref_close, high, low, close, symbol='X'):
    return [{'time': START, 'close': ref_close, 'completed': True},
            {'time': START+DAY, 'open': ref_close, 'high': high, 'low': low, 'close': close, 'completed': True}]


def test_a_day_trading_list_is_judged_on_the_next_days_real_range():
    record = {'profile': 'volatility', 'ref': {s: [START, 100.0] for s in 'AB'}, 'picks': ['A'], 'fixed': []}
    series_ = {'A': day_after(100.0, 108.0, 97.0, 101.0), 'B': day_after(100.0, 101.0, 99.5, 100.2)}
    got = u.outcome(record, series_)
    assert got['range_pick_pct'] == 11.0 and got['range_pool_pct'] == pytest.approx(6.25, abs=.01) and got['range_excess_pct'] == pytest.approx(4.75, abs=.01)
    assert got['pick_pct'] == 1.0                                       # the close-to-close numbers are still there


def test_a_month_list_gets_no_range_numbers():
    record = {'ref': {'A': [START, 100.0]}, 'picks': ['A'], 'fixed': []}
    assert 'range_pick_pct' not in u.outcome(record, {'A': day_after(100.0, 108.0, 97.0, 101.0)})


def test_the_summary_counts_the_days_the_picks_moved_more_than_the_pool():
    history = [{'market': 'US', 'result': {'pick_pct': 1.0, 'pool_pct': .5, 'excess_pool_pct': .5, 'excess_fixed_pct': None,
                                           'range_pick_pct': 9.0, 'range_pool_pct': 5.0, 'range_excess_pct': 4.0}},
               {'market': 'US', 'result': {'pick_pct': -1.0, 'pool_pct': 0.0, 'excess_pool_pct': -1.0, 'excess_fixed_pct': None,
                                           'range_pick_pct': 4.0, 'range_pool_pct': 5.0, 'range_excess_pct': -1.0}}]
    got = u.summarize(history)['US']
    assert got['range_days'] == 2 and got['range_pick_pct'] == 6.5 and got['range_pool_pct'] == 5.0 and got['wider_days'] == 1


# ---- in the engine ------------------------------------------------------------------------------------------------------------------

def quiet_market(engine):
    """Every catalogue name sits still (a 0.8% range) unless a test overrides it."""
    engine.provider.series = {c['symbol']: series(c['demo_base'], daily=.001, spread=.004) for c in CATALOGUE}


@pytest.fixture
def day(tmp_path):
    engine, store = make(tmp_path, settings=DAY_SETTINGS)
    open_now(engine)
    quiet_market(engine)
    yield engine
    store.release()


def test_a_day_trading_experiment_builds_a_volatility_list(day):
    day.provider.series.update({'NVDA': series(180, spread=.035), 'TSLA': series(400, spread=.025),
                                'AMD': series(200, spread=.02, daily=-.004),                # a downtrend that still moves a lot
                                'META': series(700, spread=.006),                            # too calm
                                'AMZN': series(220, spread=.03, daily=-.035)})               # a five-day freefall
    entry = build(day)['focus']['US']
    assert entry['profile'] == 'volatility'
    assert [p['symbol'] for p in entry['picks']] == ['NVDA', 'TSLA', 'AMD']
    why = {e['symbol']: ' '.join(e['reasons']) for e in entry['excluded']}
    assert '단타 기준' in why['META'] and '급락' in why['AMZN']
    first = entry['picks'][0]
    assert first['range_pct'] == pytest.approx(7.0, abs=.05) and first['source'] == 'data'
    assert day.store.read()['focus_history'][-1]['profile'] == 'volatility'
    assert day.public_state()['focus_config']['profile'] == 'volatility'


def test_a_month_experiment_keeps_the_trend_screen(tmp_path):
    engine, store = make(tmp_path)
    open_now(engine)
    quiet_market(engine)
    engine.provider.series.update({'NVDA': series(180, spread=.035, daily=-.004), 'META': series(700, spread=.006, daily=.006)})
    entry = build(engine)['focus']['US']
    assert entry['profile'] == 'trend' and 'META' in picks({'focus': {'US': entry}}, 'US') and 'NVDA' not in picks({'focus': {'US': entry}}, 'US')
    assert engine.public_state()['focus_config']['profile'] == 'trend'
    store.release()


def test_a_calm_market_leaves_the_list_empty_instead_of_forcing_picks(day):
    entry = build(day)['focus']['US']
    assert entry['picks'] == [] and entry['status'] in ('ok', 'quant_only')
    assert all('단타 기준' in ' '.join(e['reasons']) for e in entry['excluded'])
    assert day.buyable_symbols(day.store.read()) is not None            # an empty list is a real answer: no new buys in that market


def test_the_morning_brief_asks_for_todays_movers_and_is_shown_the_movement_numbers(day):
    day.provider.series.update({'NVDA': series(180, spread=.035), 'TSLA': series(400, spread=.025)})
    day.start()
    seen = []
    real = day.agents.run
    day.agents.run = lambda role, ctx, gen: (seen.append((role, copy.deepcopy(ctx))), real(role, ctx, gen))[1]
    build(day)
    role, ctx = next(x for x in seen if x[0] == 'trend' and x[1]['market'] == 'US')
    assert ctx['profile'] == 'volatility' and ctx['guards']['range_floor_pct']['stock'] == 1.8 and ctx['guards']['falling_knife_5d_pct'] == -15.0
    assert {c['symbol'] for c in ctx['candidates']} == {'NVDA', 'TSLA'} and all(c['range_pct'] >= 4.99 for c in ctx['candidates'])


def test_a_month_brief_is_not_told_to_look_for_day_trades(tmp_path):
    engine, store = make(tmp_path)
    open_now(engine)
    engine.start()
    seen = []
    real = engine.agents.run
    engine.agents.run = lambda role, ctx, gen: (seen.append((role, copy.deepcopy(ctx))), real(role, ctx, gen))[1]
    build(engine)
    ctx = next(c for r, c in seen if r == 'trend')
    assert ctx['profile'] == 'trend' and 'ext_20d_cap_pct' in ctx['guards'] and 'range_floor_pct' not in ctx['guards']
    store.release()


class Reply:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def system_text(monkeypatch, context):
    sent = {}
    monkeypatch.setattr(agents_module.httpx, 'post', lambda url, json, headers, timeout: sent.update(json) or Reply({'ok': False, 'message': 'stop'}))
    agents = Agents(Config(database_url='sqlite://', mode='toss', providers='claude', bridge_url='http://bridge.test',
                           bridge_token='t'*64, gemini_key=''), None)
    agents.gate.fetch = lambda: {}
    with pytest.raises(ProviderError):
        agents.run('trend', context, 1)
    return sent['system']


def test_the_ai_gets_the_day_trading_brief_only_for_a_volatility_list(monkeypatch):
    base = {'strategy_mode': 'intraday', 'candidates': [{'symbol': 'AAPL'}], 'max_picks': 1}
    day_text = system_text(monkeypatch, dict(base, profile='volatility'))
    month_text = system_text(monkeypatch, dict(base, profile='trend'))
    assert TREND_VOLATILITY_PROMPT in day_text and '오늘 하루 안에' in day_text and 'range_pct' in day_text
    assert TREND_VOLATILITY_PROMPT not in month_text and '1개월' in month_text


def test_the_same_session_selector_sees_each_candidates_daily_volatility(day):
    day.provider.series.update({'NVDA': series(180, spread=.035), 'TSLA': series(400, spread=.025), 'AMD': series(200, spread=.02)})
    build(day)
    day.set_execution('auto')
    day.start()
    seen = []
    real = day.agents.run
    day.agents.run = lambda role, ctx, gen: (seen.append((role, copy.deepcopy(ctx))), real(role, ctx, gen))[1]
    with day.store.edit() as s:
        s['next_run'] = 0
    day.cycle()
    ctx = next(c for r, c in seen if r == 'selector')
    market_names = {c['symbol']: c for c in ctx['candidates']}
    assert market_names and all('daily_volatility' in c for c in market_names.values() if c['market'] == 'US')
    nvda = market_names.get('NVDA')
    if nvda:
        assert nvda['daily_volatility']['range_pct'] == pytest.approx(7.0, abs=.05)


def test_the_selector_prompt_tells_the_ai_to_use_the_daily_volatility():
    assert 'daily_volatility' in DESK_PROMPTS['selector'] and 'range_pct' in DESK_PROMPTS['selector']


def test_yesterdays_day_trading_picks_are_scored_on_how_much_they_moved(day):
    day.provider.series.update({'NVDA': series(180, spread=.035), 'TSLA': series(400, spread=.025), 'AMD': series(200, spread=.02)})
    build(day)
    first = day.store.read()['focus_history']
    us_picks = [p for p in first if p['market'] == 'US'][0]['picks']
    with day.store.edit() as s:
        for entry in s['focus'].values():
            entry['session_date'] = '1999-12-31'
        for record in s['focus_history']:
            record['session_date'] = '1999-12-31'
    end = int(time.time()//DAY)*DAY
    for c in CATALOGUE:
        rows = day.provider.candles(c['symbol'])
        wild = c['symbol'] in us_picks
        last = rows[-1]['close']
        day.provider.series[c['symbol']] = rows+[{**rows[-1], 'time': end, 'high': last*(1.06 if wild else 1.004), 'low': last*(.95 if wild else .998), 'close': last}]
    day.focus_attempts.clear()
    day.refresh_focus()
    record = [r for r in day.store.read()['focus_history'] if r['market'] == 'US' and r['session_date'] == '1999-12-31'][0]
    assert record['profile'] == 'volatility'
    assert record['result']['range_pick_pct'] == pytest.approx(11.0, abs=.1) and record['result']['range_excess_pct'] > 5
    summary = day.public_state()['focus_eval']['US']
    assert summary['range_days'] == 1 and summary['wider_days'] == 1
