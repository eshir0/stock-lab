"""Score every intraday AI decision against later prices and simple baselines.

A decision is recorded when the director finishes, whether or not it trades. The refresh loop
later fills in the mid price 30 and 60 minutes on (or at the session close if that comes first).
Nothing here changes trading; it only measures whether the AI's calls beat doing nothing.
"""
import math

from .rules import RULES

HORIZONS = (30, 60)
MAX_RECORDS = 1000
LATE_LIMIT = 600
MIN_SAMPLE = 30


def mid(quote):
    try:
        bid, ask = float(quote['bid']), float(quote['ask'])
    except (KeyError, TypeError, ValueError):
        return None
    return (bid+ask)/2 if bid > 0 and ask > 0 and math.isfinite(bid+ask) else None


def record_decision(s, *, run_id, symbol, market, decision, quote, candidates, selected_by, cost_bps, now, rules=None):
    price = mid(quote)
    if price is None:
        return None
    entry = {'run_id': run_id, 'time': now, 'symbol': symbol, 'market': market,
             'stance': decision.get('stance'), 'target_weight_pct': decision.get('target_weight_pct'),
             'engine': str(decision.get('engine') or ''), 'selected_by': selected_by, 'action': 'hold',
             'price': price, 'session_end': quote.get('session_end'), 'cost_bps': round(cost_bps, 2),
             'candidates': {k: v for k, v in candidates.items() if v}, 'rules': dict(rules or {}), 'outcomes': {}}
    records = s.setdefault('evaluations', [])
    records.append(entry)
    del records[:-MAX_RECORDS]
    return entry


def _returns(entry, quotes):
    prices = {entry['symbol']: entry['price'], **entry.get('candidates', {})}
    out = {}
    for symbol, start in prices.items():
        now_price = mid(quotes.get(symbol, {}))
        if now_price and start:
            out[symbol] = round((now_price/start-1)*100, 4)
    return out


def update_outcomes(s, now, quote_age=30):
    """Fill due horizons from the freshest cached quotes; never back-fill stale or distant prices."""
    quotes = s.get('quotes', {})
    for entry in s.get('evaluations', []):
        for horizon in HORIZONS:
            key = str(horizon)
            if key in entry['outcomes']:
                continue
            due = entry['time']+horizon*60
            end = entry.get('session_end') or due
            quote = quotes.get(entry['symbol'], {})
            asof = quote.get('asof', 0)
            if due <= end:
                if now < due:
                    continue
                fresh = now-quote.get('received', 0) <= quote_age and asof >= due-quote_age
                if fresh and now-due <= LATE_LIMIT:
                    entry['outcomes'][key] = {'time': now, 'at_close': False, 'returns': _returns(entry, quotes)}
                elif now-due > LATE_LIMIT:
                    entry['outcomes'][key] = {'time': now, 'missed': True}
            elif now >= end:
                # The session closed before this horizon: score at the last quote seen before the close.
                if end-LATE_LIMIT <= asof <= end+60:
                    entry['outcomes'][key] = {'time': asof, 'at_close': True, 'returns': _returns(entry, quotes)}
                elif now-end > LATE_LIMIT:
                    entry['outcomes'][key] = {'time': now, 'missed': True}


def _avg(values):
    return round(sum(values)/len(values), 4) if values else None


def _rate(flags):
    return round(sum(flags)/len(flags)*100, 1) if flags else None


def _net(stance, move, cost_bps):
    """Result of acting on a stance: buy earns the move, sell avoids it, hold earns nothing; trades pay costs."""
    if stance == 'BUY':
        return move-cost_bps/100
    if stance == 'SELL':
        return -move-cost_bps/100
    return 0.0


def _rule_rows(scored, move, key):
    """Each rule scored on the same decision moments and costs as the AI (only where it could be computed)."""
    rows = {}
    for name in RULES:
        subset = [e for e in scored if (e.get('rules') or {}).get(name) in ('BUY', 'SELL', 'HOLD')]
        trades = [e for e in subset if e['rules'][name] in ('BUY', 'SELL')]
        hits = [(move[id(e)] > e['cost_bps']/100) if e['rules'][name] == 'BUY' else (move[id(e)] < -e['cost_bps']/100)
                for e in trades]
        rows[name] = {'count': len(subset), 'trades': len(trades),
                      'avg_net_pct': _avg([_net(e['rules'][name], move[id(e)], e['cost_bps']) for e in subset]),
                      'ai_same_avg_net_pct': _avg([_net(e['stance'], move[id(e)], e['cost_bps']) for e in subset]),
                      'hit_rate_pct': _rate(hits)}
    return rows


def summarize(records):
    """Plain numbers for the dashboard: what the AI did versus 'always hold' and 'always buy'."""
    horizons = {}
    for horizon in HORIZONS:
        key = str(horizon)
        scored = [e for e in records if 'returns' in e['outcomes'].get(key, {})
                  and e['symbol'] in e['outcomes'][key]['returns']]
        move = {id(e): e['outcomes'][key]['returns'][e['symbol']] for e in scored}
        buys = [e for e in scored if e['stance'] == 'BUY']
        sells = [e for e in scored if e['stance'] == 'SELL']
        holds = [e for e in scored if e['stance'] == 'HOLD']
        ai = [_net(e['stance'], move[id(e)], e['cost_bps']) for e in scored]
        always_buy = [move[id(e)]-e['cost_bps']/100 for e in scored]
        picks = []
        for e in scored:
            others = [abs(v) for sym, v in e['outcomes'][key]['returns'].items() if sym != e['symbol']]
            if e['selected_by'] == 'ai' and others:
                picks.append((abs(move[id(e)]), _avg(others)))
        horizons[key] = {
            'scored': len(scored),
            'buy': {'count': len(buys), 'avg_return_pct': _avg([move[id(e)] for e in buys]),
                    'avg_net_pct': _avg([move[id(e)]-e['cost_bps']/100 for e in buys]),
                    'hit_rate_pct': _rate([move[id(e)] > e['cost_bps']/100 for e in buys])},
            'sell': {'count': len(sells), 'avg_avoided_pct': _avg([-move[id(e)] for e in sells]),
                     'hit_rate_pct': _rate([move[id(e)] < 0 for e in sells])},
            'hold': {'count': len(holds), 'avg_return_pct': _avg([move[id(e)] for e in holds]),
                     'avg_abs_move_pct': _avg([abs(move[id(e)]) for e in holds]),
                     'missed_gain_rate_pct': _rate([move[id(e)] > e['cost_bps']/100 for e in holds])},
            'ai_avg_net_pct': _avg(ai), 'always_hold_pct': 0.0 if scored else None,
            'rules': _rule_rows(scored, move, key),
            'always_buy_avg_net_pct': _avg(always_buy),
            'selector': {'count': len(picks), 'chosen_abs_move_pct': _avg([p[0] for p in picks]),
                         'others_abs_move_pct': _avg([p[1] for p in picks]),
                         'bigger_mover_rate_pct': _rate([p[0] > p[1] for p in picks])},
        }
    engines = {}
    for e in records:
        name = e['engine'].split(' · ')[0] or '알 수 없음'
        engines.setdefault(name, {'decisions': 0, 'trades': 0})
        engines[name]['decisions'] += 1
        engines[name]['trades'] += e['stance'] in ('BUY', 'SELL')
    counts = {stance: sum(e['stance'] == stance for e in records) for stance in ('BUY', 'SELL', 'HOLD')}
    scored60 = horizons[str(HORIZONS[-1])]['scored']
    return {'decisions': len(records), 'counts': counts, 'horizons': horizons, 'engines': engines,
            'pending': sum(len(e['outcomes']) < len(HORIZONS) for e in records),
            'min_sample': MIN_SAMPLE, 'enough_sample': scored60 >= MIN_SAMPLE}
