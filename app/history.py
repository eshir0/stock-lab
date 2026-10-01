"""What the AI is shown about the past: up to three months of daily bars, summarised by the server.

Pure functions over daily candles ({'time', 'open', 'high', 'low', 'close', 'volume', 'completed'}). The numbers are
computed here rather than by the model, and only from completed days, so a session still in progress never leaks in.
Nothing in this module can place or size an order.
"""
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HISTORY_DAYS = 92     # calendar days of history offered to the analysts: about three months
FETCH_BARS = 70       # sessions to read; 70 always cover 92 calendar days (the API allows up to 200)
MIN_BARS = 23         # what the slowest rule (golden cross) needs; fewer bars means the rules cannot be computed
COLUMNS = ('date', 'open', 'high', 'low', 'close', 'volume')
KST = ZoneInfo('Asia/Seoul')


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _pct(new, old):
    return round((new/old-1)*100, 2) if old else None


def day_label(timestamp):
    """Calendar date of a daily bar. Bars are stamped at local midnight (or UTC midnight), so twelve hours later is
    always inside the right date whatever the exchange's zone."""
    return datetime.fromtimestamp(timestamp+43200, timezone.utc).date().isoformat()


def completed_bars(candles, now=None, days=HISTORY_DAYS):
    """Valid, completed, de-duplicated bars, oldest first, limited to the last `days` calendar days."""
    seen, rows = set(), []
    for c in candles or []:
        try:
            ok = (c.get('completed') and _finite(c.get('close')) and c['close'] > 0
                  and _finite(c.get('volume')) and c['volume'] >= 0 and _finite(c.get('time')))
        except AttributeError:
            ok = False
        if ok and c['time'] not in seen:
            seen.add(c['time'])
            rows.append(c)
    rows.sort(key=lambda c: c['time'])
    if now is not None:
        rows = [c for c in rows if c['time'] >= now-days*86400]
    return rows


def _true_range(bar, previous):
    high = bar['high'] if _finite(bar.get('high')) else bar['close']
    low = bar['low'] if _finite(bar.get('low')) else bar['close']
    return max(high-low, abs(high-previous['close']), abs(low-previous['close']))


def summary(bars):
    """Numbers the analysts should not have to compute themselves. None means the window is too short for it."""
    n = len(bars)
    if not n:
        return {'sessions': 0}
    closes = [b['close'] for b in bars]
    volumes = [b['volume'] for b in bars]
    last = closes[-1]
    highs = [b['high'] if _finite(b.get('high')) else b['close'] for b in bars]
    lows = [b['low'] if _finite(b.get('low')) else b['close'] for b in bars]
    top, bottom = max(range(n), key=lambda i: highs[i]), min(range(n), key=lambda i: lows[i])
    sma = lambda k: round(sum(closes[-k:])/k, 4) if n >= k else None
    sma20, sma60 = sma(20), sma(60)
    ranges = [_true_range(b, a) for a, b in zip(bars[-15:-1], bars[-14:])]
    moves = [_pct(b, a) for a, b in zip(closes[-21:], closes[-20:])]
    peak, drawdown = closes[0], 0.0
    for close in closes:
        peak = max(peak, close)
        drawdown = min(drawdown, (close/peak-1)*100)
    return {
        'sessions': n, 'from': day_label(bars[0]['time']), 'to': day_label(bars[-1]['time']), 'last_close': round(last, 4),
        'ret_1w_pct': _pct(last, closes[-6]) if n >= 6 else None,
        'ret_1m_pct': _pct(last, closes[-22]) if n >= 22 else None,
        'ret_3m_pct': _pct(last, closes[0]) if n >= 40 else None,
        'high': round(highs[top], 4), 'high_date': day_label(bars[top]['time']),
        'low': round(lows[bottom], 4), 'low_date': day_label(bars[bottom]['time']),
        'from_high_pct': _pct(last, highs[top]), 'from_low_pct': _pct(last, lows[bottom]),
        'sma5': sma(5), 'sma20': sma20, 'sma60': sma60,
        'gap_sma20_pct': _pct(last, sma20) if sma20 else None, 'gap_sma60_pct': _pct(last, sma60) if sma60 else None,
        'atr_pct': round(sum(ranges)/len(ranges)/last*100, 2) if len(ranges) == 14 else None,
        'avg_volume_20d': round(sum(volumes[-20:])/20) if n >= 20 else None,
        'volume_ratio_5d': round(sum(volumes[-5:])/5/(sum(volumes[-20:])/20), 2) if n >= 20 and sum(volumes[-20:]) > 0 else None,
        'up_day_ratio_20d': round(sum(1 for m in moves if m and m > 0)/len(moves), 2) if len(moves) == 20 else None,
        'max_drawdown_pct': round(drawdown, 2)}


def block(bars):
    """The payload for the analysts: the summary plus every bar as a compact row, or None without any bars."""
    if not bars:
        return None

    def price(value):
        return round(value, 4) if _finite(value) else None
    rows = [[day_label(b['time']), price(b.get('open')), price(b.get('high')), price(b.get('low')), price(b['close']),
             round(b['volume'])] for b in bars]
    return {'note': '완료된 일봉만 포함합니다(진행 중인 오늘 봉 제외). 조정주가, 오래된 날짜부터 정렬.',
            'summary': summary(bars), 'columns': list(COLUMNS), 'rows': rows}


def minute_block(candles, limit=30):
    """The last few completed one-minute bars as compact rows: enough to see whether the price is spiking right now."""
    bars = [c for c in candles or [] if isinstance(c, dict) and _finite(c.get('close')) and _finite(c.get('time'))][-limit:]
    if not bars:
        return None

    def price(value):
        return round(value, 4) if _finite(value) else None
    rows = [[datetime.fromtimestamp(b['time'], KST).strftime('%H:%M'), price(b.get('open')), price(b.get('high')),
             price(b.get('low')), price(b['close']), round(b['volume']) if _finite(b.get('volume')) else None] for b in bars]
    return {'interval': '1m', 'timezone': 'KST', 'note': f'오늘 완료된 1분봉 중 최근 {len(rows)}개. 진입 시점의 급등락 확인용입니다.',
            'columns': ['time', 'open', 'high', 'low', 'close', 'volume'], 'rows': rows}


def own_records(state, symbol, now, days=HISTORY_DAYS, limit=6):
    """This experiment's own earlier calls and fills on the symbol, so the analysts see what was already tried."""
    since = now-days*86400
    decisions = []
    for e in (state.get('evaluations') or []):
        if e.get('symbol') != symbol or e.get('time', 0) < since:
            continue
        after = {}
        for key, outcome in (e.get('outcomes') or {}).items():
            value = (outcome.get('returns') or {}).get(symbol) if isinstance(outcome, dict) else None
            if value is not None:
                after[key] = value
        decisions.append({'date': datetime.fromtimestamp(e['time'], KST).strftime('%Y-%m-%d %H:%M'),
                          'stance': e.get('stance'), 'price': e.get('price'), 'action': e.get('action'),
                          'return_after_pct': after})
    trades = [{'date': datetime.fromtimestamp(t['time'], KST).strftime('%Y-%m-%d %H:%M'), 'side': t.get('side'),
               'quantity': t.get('quantity'), 'price': t.get('price'), 'realized': t.get('realized'),
               'exit_reason': t.get('exit_reason') or ''}
              for t in (state.get('trades') or []) if t.get('symbol') == symbol and t.get('time', 0) >= since]
    return {'decisions': decisions[-limit:], 'trades': trades[-limit:]}
