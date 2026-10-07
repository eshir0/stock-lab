"""research_plan_market.json (2026-10-07): US names get a server-set wide stop (3x the daily range) and target (3x the stop)
in experiments with exit_profile='market'; Korean names keep the director's numbers; saved experiments keep 'ai'."""
import pytest

from app import history
from app.risk import RiskError, market_exit, normalize_settings
from test_month_desk import SETTINGS, bars, desk, run_cycle, series  # noqa: F401  (fixture)


def test_the_formula_and_its_bounds():
    assert market_exit('US', 2.5) == (7.5, 22.5)
    assert market_exit('US', 0.4) == (2.0, 6.0)                 # the stop never goes below 2%
    assert market_exit('US', 6.0) == (15.0, 40.0)               # nor above 15%, and the target stays within 40%
    assert market_exit('KR', 2.5) is None and market_exit('US', None) is None and market_exit('US', 0) is None


def test_saved_experiments_keep_the_ai_choice_and_new_values_are_checked():
    base = {'horizon': 'month'}
    assert normalize_settings(base)['exit_profile'] == 'ai'
    assert normalize_settings({**base, 'exit_profile': 'market'})['exit_profile'] == 'market'
    with pytest.raises(RiskError):
        normalize_settings({**base, 'exit_profile': 'wide'})


def test_a_us_buy_uses_the_wide_stop_and_a_korean_buy_does_not(desk):
    with desk.store.edit() as s:
        s['strategy_settings']['exit_profile'] = 'market'
    us = bars(series(200, 'up'))
    kr = bars(series(70000, 'up'))
    desk.provider.daily['AAPL'], desk.provider.daily['005930'] = us, kr
    state = run_cycle(desk)
    state = run_cycle(desk) if 'AAPL' not in state['positions'] else state
    expected = market_exit('US', history.summary(history.completed_bars(us)).get('atr_pct'))
    assert expected and 'AAPL' in state['positions']
    assert state['positions']['AAPL']['trail_pct'] == expected[0]                    # the trail follows the wide stop
    director = [ctx for role, ctx in desk.contexts if role == 'director']
    assert any('stop_rule' in ctx['constraints'] for ctx in director if ctx['symbol'] == 'AAPL')
    assert all('stop_rule' not in ctx['constraints'] for ctx in director if ctx['symbol'] == '005930')
    if '005930' in state['positions']:
        assert state['positions']['005930']['trail_pct'] != market_exit('US', history.summary(history.completed_bars(kr)).get('atr_pct'))[0]


def test_market_long_holds_us_names_for_ninety_days_and_korean_names_as_before(desk):
    """research_plan_us_hold.json part A (2026-10-07): with stops this wide 62% of trades ended at the 21-session limit;
    63 sessions passed the pre-registered test, so 'market_long' gives US names 90 calendar days."""
    import time
    from app.risk import US_LONG_HOLD_MINUTES
    assert normalize_settings({'horizon': 'month', 'exit_profile': 'market_long'})['exit_profile'] == 'market_long'
    with desk.store.edit() as s:
        s['strategy_settings']['exit_profile'] = 'market_long'
    desk.provider.daily['AAPL'], desk.provider.daily['005930'] = bars(series(200, 'up')), bars(series(70000, 'up'))
    state = run_cycle(desk)
    state = run_cycle(desk) if 'AAPL' not in state['positions'] else state
    us = state['positions']['AAPL']
    assert us['expires_at']-time.time() == pytest.approx(US_LONG_HOLD_MINUTES*60, abs=600)
    if '005930' in state['positions']:
        assert state['positions']['005930']['expires_at']-time.time() <= 30*86400+600
    director = [ctx for role, ctx in desk.contexts if role == 'director' and ctx['symbol'] == 'AAPL']
    assert director and 'holding_rule' in director[-1]['constraints'] and 'stop_rule' in director[-1]['constraints']



def test_a_holding_period_beyond_the_range_is_clamped_not_refused():
    """2026-10-07 22:52: told about the 90-day US hold, both AIs answered 129,600 minutes and the analysis failed."""
    from test_entry import director, judged
    assert judged(director(max_holding_minutes=129600), horizon='month')['max_holding_minutes'] == 43200
    assert judged(director(max_holding_minutes=10), horizon='month')['max_holding_minutes'] == 1440
    assert judged(director(max_holding_minutes=20160.4), horizon='month')['max_holding_minutes'] == 20160
    with pytest.raises(ValueError):
        judged(director(max_holding_minutes='90일'), horizon='month')
