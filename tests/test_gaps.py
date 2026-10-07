"""Gap stops (2026-10-07): a stop crossed by an opening gap fills at the first price, like a resting stop order at a broker
would, and the report shows how far past the stop such exits filled."""
import pytest

from app import scorecard
from test_month_desk import desk, open_position  # noqa: F401  (fixture)


def test_a_gap_below_the_stop_fills_at_the_open_and_is_recorded(desk):
    open_position(desk, '005930', price=100000.0, stop_pct=5, quantity=1)
    desk.provider.prices['005930'] = 90000.0                       # opened 5.3% under the 95,000 stop
    desk.refresh()
    desk.process_desk_exits()
    state = desk.store.read()
    sell = next(t for t in state['trades'] if t['side'] == 'SELL')
    assert sell['price'] < 95000 and sell['gap_pct'] == pytest.approx((90000/95000-1)*100, abs=.05)
    assert any('갭: 손절가보다' in e['message'] for e in state['events'])
    gaps = scorecard.report(state['trades'])['gaps']
    assert gaps['count'] == 1 and gaps['avg_pct'] == pytest.approx(sell['gap_pct'], abs=.01)


def test_a_stop_touched_during_the_session_is_not_a_gap(desk):
    open_position(desk, '005930', price=100000.0, stop_pct=5, quantity=1)
    desk.provider.prices['005930'] = 94900.0                       # 0.1% under the stop: touched, not gapped
    desk.refresh()
    desk.process_desk_exits()
    sell = next(t for t in desk.store.read()['trades'] if t['side'] == 'SELL')
    assert 'gap_pct' not in sell and scorecard.report(desk.store.read()['trades'])['gaps']['count'] == 0
