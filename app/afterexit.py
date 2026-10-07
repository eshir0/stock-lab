"""After-exit tracking: what happened to a name after the desk sold it. Measurement only - it never changes a trade.

Every closed month round trip that ended through an exit rule (take-profit, stop, trailing stop, expiry, rotation) is
followed on completed daily bars for 20 trading days. Two questions get answered from real data instead of opinion:

- After any exit, did the price keep going (d5 / d20 close against the exit price)? A steady rise after selling is the
  "sold the winner too early" pattern; a fall means the exit protected the profit.
- For a take-profit exit: what would "keep holding and follow the high with the same trailing stop" (option C) have
  earned compared with selling at the target (the current rule, option A)? Replayed bar by bar, conservatively: a bar's
  low is checked against the stop before its high may raise the stop, a gap below the stop fills at the open, and the
  position's own expiry still ends it.

Each record keeps the setup that bought it - trend rules (golden cross, momentum, breakout), the mean-reversion rule, or
both - so the result can be split by kind: if trend buys gain from C while range buys do not, a later rule could choose
by kind. The summary is a reference figure, not a verification check, and is not part of the strategy fingerprint.
"""
from .instruments import SYMBOLS
from . import scorecard, splits

HORIZONS = (5, 20)
KEEP = 200
REPLAY_BARS = 30                 # a month position never lives longer than this many sessions
TREND_RULES = ('golden_cross', 'momentum', 'breakout')
STYLES = {'trend': '추세형 신호', 'range': '평균회귀 신호', 'mixed': '두 신호 모두', 'unknown': '신호 정보 없음'}
TAKE = '익절 조건'


def style_of(signals):
    signals = signals or {}
    trend = any(signals.get(name) == 'BUY' for name in TREND_RULES)
    reverting = signals.get('mean_reversion') == 'BUY'
    return 'mixed' if trend and reverting else 'trend' if trend else 'range' if reverting else 'unknown'


def _setup(state, symbol, opened, buy):
    """What bought the position: the latest analysis of that name at or before the first buy (a conditional entry's own
    record carries no rules, so it is skipped and the analysis that left the plan is used), and how it was entered."""
    analysis = next((e for e in reversed(state.get('evaluations') or [])
                     if e.get('symbol') == symbol and e.get('selected_by') != 'watch' and e.get('time', 0) <= opened+1), None)
    entry_type = 'analysis'
    if buy and buy.get('entry_watch'):
        watch = next((w for w in state.get('watches') or [] if w.get('id') == buy['entry_watch']), None)
        entry_type = (watch or {}).get('type') or 'watch'
    signals = dict((analysis or {}).get('rules') or {})
    return {'style': style_of(signals), 'signals': signals, 'entry': entry_type}


def record(state, symbol, position, trade, reason):
    """Start following a round trip that an exit rule just closed. `position` is the position as it was before the final
    sell (its stop, trail, high and expiry); `trade` is that sell."""
    if not position or SYMBOLS.get(symbol) is None:
        return None
    closed, _ = scorecard.round_trips(state.get('trades') or [])
    trip = next((t for t in reversed(closed) if t['symbol'] == symbol and t['closed'] == trade['time']), None)
    if trip is None:
        return None
    buy = next((t for t in state['trades'] if t.get('symbol') == symbol and t.get('side') == 'BUY'
                and t.get('time') == trip['opened']), None)
    item = {'id': trade['id'], 'symbol': symbol, 'name': SYMBOLS[symbol]['name'], 'currency': SYMBOLS[symbol]['currency'],
            'opened': trip['opened'], 'time': trade['time'], 'reason': reason, 'exit_price': float(trade['price']),
            'quantity': trade['quantity'], 'trip_return_pct': trip['return_pct'],
            **_setup(state, symbol, trip['opened'], buy), 'after': {}}
    if reason == TAKE:
        item['replay'] = {key: position.get(key) for key in ('average', 'stop_price', 'trail_pct', 'high_water', 'expires_at')}
    elif position.get('exit_mode') == 'trail' and position.get('target_hit') and position.get('take_profit_price'):
        # The other way round: this experiment kept the position past its target, so what selling AT the target (A) would
        # have given is exactly known - the target, a resting limit - and C - A is the actual exit against it.
        take = float(position['take_profit_price'])
        item['target_hit'] = True
        item['c'] = {'exit_price': item['exit_price'], 'why': '실제 추적 손절 청산 vs 익절가 매도', 'a_price': take,
                     'extra_pct': round((item['exit_price']/take-1)*100, 4)}
    records = state.setdefault('after_exits', [])
    records.append(item)
    del records[:-KEEP]
    return item


def _done(item):
    return all(f'd{d}' in item['after'] for d in HORIZONS) and ('replay' not in item or 'c' in item)


def due(state):
    """Symbols whose daily bars are still needed."""
    return {item['symbol'] for item in state.get('after_exits') or [] if not _done(item)}


def replay_trailing(item, bars, now):
    """Option C for a take-profit exit: keep the position at the target and follow the high with the same trailing stop
    until the stop is hit or the position's expiry. None until the bars settle it."""
    plan = item.get('replay') or {}
    trail = plan.get('trail_pct')
    if not isinstance(trail, (int, float)) or trail <= 0:
        return {'skipped': '추적 손절 정보 없음'}
    price = item['exit_price']
    high = max(plan.get('high_water') or 0, price)
    stop = max(plan.get('stop_price') or 0, (plan.get('average') or 0)*1.003, high*(1-trail/100))
    expires = plan.get('expires_at') or item['time']+REPLAY_BARS*86400
    last = None
    for n, bar in enumerate(bars, 1):
        if bar['time'] >= expires:
            return {'exit_price': last['close'] if last else price, 'why': '최대 보유시간', 'bars': n-1}
        low, top = bar.get('low', bar['close']), bar.get('high', bar['close'])
        opening = bar.get('open', bar['close'])
        if opening <= stop:
            return {'exit_price': opening, 'why': '추적 손절(갭)', 'bars': n}
        if low <= stop:
            return {'exit_price': round(stop, 4), 'why': '추적 손절', 'bars': n}
        high = max(high, top)
        stop = max(stop, high*(1-trail/100))
        last = bar
        if n >= REPLAY_BARS:
            return {'exit_price': bar['close'], 'why': '최대 보유시간', 'bars': n}
    if now > expires+21*86400:
        return {'missed': True}
    return None


def update(state, bars_by_symbol, now):
    """Fill what the bars can settle. Returns how many results were added."""
    added = 0
    for item in state.get('after_exits') or []:
        bars = bars_by_symbol.get(item['symbol'])
        if bars is None or _done(item):
            continue
        later = [b for b in bars if b['time'] > item['time']]
        f = splits.factor(state, item['symbol'], item['time'])         # the bars are split-adjusted, the record is not
        exit_price = item['exit_price']/f
        for days in HORIZONS:
            key = f'd{days}'
            if key in item['after']:
                continue
            if len(later) >= days:
                item['after'][key] = round((later[days-1]['close']/exit_price-1)*100, 4)
                added += 1
            elif now > item['time']+(2*days+7)*86400:
                item['after'][key] = None                  # the bars never arrived
                added += 1
        if 'replay' in item and 'c' not in item:
            plan = {k: (v/f if k != 'trail_pct' and isinstance(v, (int, float)) and k != 'expires_at' else v)
                    for k, v in item['replay'].items()}
            result = replay_trailing(dict(item, exit_price=exit_price, replay=plan), later, now)
            if result is not None:
                if 'exit_price' in result:
                    result['extra_pct'] = round((result['exit_price']/exit_price-1)*100, 4)
                item['c'] = result
                added += 1
    return added


def _avg(values):
    return round(sum(values)/len(values), 4) if values else None


def summary(state):
    """The dashboard view: per setup style, how far prices went after the exit and how C compares with A."""
    records = state.get('after_exits') or []
    rows = []
    for style, label in STYLES.items():
        mine = [r for r in records if r.get('style') == style]
        if not mine:
            continue
        settled = [r['c']['extra_pct'] for r in mine if 'extra_pct' in (r.get('c') or {})]
        rows.append({'style': style, 'label': label, 'exits': len(mine),
                     'd5_avg_pct': _avg([r['after']['d5'] for r in mine if r['after'].get('d5') is not None]),
                     'd20_avg_pct': _avg([r['after']['d20'] for r in mine if r['after'].get('d20') is not None]),
                     'takes': sum(r['reason'] == TAKE or bool(r.get('target_hit')) for r in mine), 'replayed': len(settled),
                     'c_minus_a_avg_pct': _avg(settled), 'c_better': sum(x > 0 for x in settled)})
    recent = [{k: r.get(k) for k in ('symbol', 'name', 'time', 'reason', 'exit_price', 'style', 'entry', 'after', 'c')}
              for r in records[-8:]]
    return {'tracked': len(records), 'waiting': sum(not _done(r) for r in records), 'rows': rows, 'recent': recent}
