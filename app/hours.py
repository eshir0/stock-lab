"""Regular trading hours per market: a fixed second lock on every paper fill, on top of the broker's own calendar.

The broker's market calendar already decides holidays, half days and the session of the day. This module only says the
outer bound that never changes: Korea 09:00-15:20 Seoul time (the 15:20-15:30 closing auction and the pre/after-market
sessions are excluded), the US 09:30-16:00 New York time (pre-market and after-hours excluded; daylight saving follows the
New York clock). Weekends are always closed. A fill outside these hours is refused whatever the calendar says.
"""
from datetime import datetime, time
from zoneinfo import ZoneInfo

REGULAR = {'KR': ('Asia/Seoul', time(9, 0), time(15, 20), '국내'),
           'US': ('America/New_York', time(9, 30), time(16, 0), '미국')}
CLOSING = {'KR': time(15, 30), 'US': time(16, 0)}     # when the day's official closing price is set (after KR's auction)


def regular_open(market, now):
    zone, start, end, _ = REGULAR[market]
    local = datetime.fromtimestamp(now, ZoneInfo(zone))
    return local.weekday() < 5 and start <= local.time() < end


def closing_time(market, day):
    """Unix time at which the official close of `day` (a date) is known in `market`."""
    return datetime.combine(day, CLOSING[market], ZoneInfo(REGULAR[market][0])).timestamp()


def window_kst(market, now):
    """Today's regular hours of `market` in Seoul time, e.g. '22:30~05:00' (the US window moves with daylight saving)."""
    zone, start, end, _ = REGULAR[market]
    day = datetime.fromtimestamp(now, ZoneInfo(zone)).date()
    seoul = ZoneInfo('Asia/Seoul')
    a, b = (datetime.combine(day, t, ZoneInfo(zone)).astimezone(seoul) for t in (start, end))
    return f'{a:%H:%M}~{b:%H:%M}'


def refusal(market, now):
    return f'{REGULAR[market][3]} 정규장({window_kst(market, now)}, 한국 시간)에만 매수·매도합니다. 정규장을 기다립니다.'


def summary(now):
    return {market: {'label': REGULAR[market][3], 'kst': window_kst(market, now)} for market in REGULAR}
