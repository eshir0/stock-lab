"""Buys and sells happen only in each market's regular session: Korea 09:00-15:20 Seoul, the US 09:30-16:00 New York
(pre/after-market and the Korean closing auction excluded), as a fixed lock on top of the broker calendar."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import hours
from app.engine import RuleError
from test_entry_desk import build

SEOUL, NY = ZoneInfo('Asia/Seoul'), ZoneInfo('America/New_York')


def ts(zone, *args):
    return datetime(*args, tzinfo=zone).timestamp()


@pytest.mark.parametrize('when, ok', [
    ((2026, 10, 2, 8, 59, 59), False),        # NXT pre-market
    ((2026, 10, 2, 9, 0), True),
    ((2026, 10, 2, 15, 19, 59), True),
    ((2026, 10, 2, 15, 20), False),           # closing auction
    ((2026, 10, 2, 17, 0), False),            # after-market
    ((2026, 10, 3, 10, 0), False),            # Saturday
])
def test_korea(when, ok):
    assert hours.regular_open('KR', ts(SEOUL, *when)) is ok


@pytest.mark.parametrize('when, ok', [
    ((2026, 10, 1, 9, 29, 59), False),        # pre-market
    ((2026, 10, 1, 9, 30), True),
    ((2026, 10, 1, 15, 59, 59), True),
    ((2026, 10, 1, 16, 0), False),            # after-hours
    ((2026, 10, 4, 11, 0), False),            # Sunday
    ((2026, 12, 1, 9, 30), True),             # winter time: same New York clock
])
def test_us(when, ok):
    assert hours.regular_open('US', ts(NY, *when)) is ok


def test_the_us_window_in_seoul_time_follows_daylight_saving():
    assert hours.window_kst('US', ts(NY, 2026, 10, 1, 12)) == '22:30~05:00'
    assert hours.window_kst('US', ts(NY, 2026, 12, 1, 12)) == '23:30~06:00'
    assert hours.window_kst('KR', ts(SEOUL, 2026, 12, 1, 12)) == '09:00~15:20'


def test_a_korean_fill_in_seoul_after_the_us_close_is_not_judged_by_the_us_clock():
    now = ts(SEOUL, 2026, 10, 2, 10, 0)       # Korea open, the US closed
    assert hours.regular_open('KR', now) and not hours.regular_open('US', now)


@pytest.fixture
def engine(tmp_path):
    engine, store = build(tmp_path)
    yield engine
    store.release()


def test_every_fill_is_refused_outside_regular_hours(engine, monkeypatch):
    engine.hours_lock = True
    seen = []
    monkeypatch.setattr(hours, 'regular_open', lambda market, now: seen.append(market) or False)
    s = engine.store.read()
    q = s['quotes']['005930']
    for side in ('BUY', 'SELL'):
        with pytest.raises(RuleError, match='국내 정규장'):
            engine.fill(s, '005930', side, 1, q, 'test')
    assert seen == ['KR', 'KR']


def test_inside_regular_hours_the_lock_lets_the_fill_through(engine, monkeypatch):
    engine.hours_lock = True
    monkeypatch.setattr(hours, 'regular_open', lambda market, now: True)
    s = engine.store.read()
    engine.fill(s, '005930', 'BUY', 1, s['quotes']['005930'], 'test')
    assert s['positions']['005930']['quantity'] == 1


def test_the_lock_is_on_for_real_quotes_and_shown_on_the_dashboard(engine):
    from app.config import Config
    assert Config(mode='toss').mode == 'toss' and engine.hours_lock is False           # demo quotes carry no session
    import app.engine as module
    assert "self.hours_lock = config.mode != 'demo'" in open(module.__file__, encoding='utf-8').read()
    assert set(engine.public_state()['config']['regular_hours']) == {'KR', 'US'}
