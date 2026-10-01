"""Conditional entries: the director's "buy there, not now", watched by the server without any AI call.

The director may answer HOLD together with a price plan: a breakout (buy when the price climbs to a level) or a pullback (buy
when it falls back to a level), the price at which the idea is dead, and how long the plan stays valid. The server keeps the plan
as a watch. On every quote poll it only compares the live price with those numbers; when the condition holds it sizes and fills
the order through the same risk rules as an analysed BUY. Nothing in this module can place an order or call the AI.

Pure functions over plain dicts; the desk (`desk.py`) does the quotes, the state and the order.
"""
import math
import uuid

TYPES = ('breakout', 'pullback')
LABELS = {'breakout': '돌파 매수', 'pullback': '눌림 매수'}
FIELDS = ('entry_type', 'entry_level', 'entry_invalidate', 'entry_minutes')

# Everything a plan has to satisfy. The plan is the AI's; these limits are the server's.
MINUTES = {'intraday': (10, 180), 'month': (30, 1440)}     # how long a plan may wait for its price
MAX_AWAY_PCT = {'intraday': 8.0, 'month': 15.0}             # how far from the current price the level may sit
MAX_DEPTH_PCT = 15.0                                        # how far below the level the plan is still considered alive
CLOSE_MARGIN = 600                                          # no plan is left waiting into the last 10 minutes of a same-session day
CHASE_PCT = .5                                              # a breakout is bought at most this far above its level: no chasing a spike
CONFIRM_POLLS = 2                                           # fresh quotes in a row that must meet the condition before an order
VOLUME_RATIO = 1.0                                          # a breakout also needs the last 5 minutes' volume at least this x the 20 before
VOLUME_RECHECK = 30                                         # seconds before a breakout whose volume was not enough is looked at again
MAX_FAILURES = 6                                            # refused or failed attempts (10 s apart) before a triggered plan is dropped
MAX_WAITING = 3                                             # plans waiting at once
KEEP = 60                                                   # finished watches kept in the ledger
RESTART_GRACE = 300                                         # a restart this soon after the last quote keeps the waiting plans (a deploy); a longer outage drops them

FINISHED = {'filled': '체결', 'proposed': '승인 대기', 'expired': '기한 만료', 'invalid': '무효 가격 이탈',
            'replaced': '새 분석으로 교체', 'blocked': '위험 규칙으로 보류', 'cancelled': '취소'}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def waiting(state):
    return [w for w in state.get('watches') or [] if w.get('status') == 'waiting']


def find(state, watch_id):
    return next((w for w in state.get('watches') or [] if w.get('id') == watch_id), None)


def fmt_price(value, currency):
    return f'{value:,.0f}원' if currency == 'KRW' else f'${value:,.2f}'


def describe(watch):
    """One line: what the server is waiting for."""
    currency = watch['currency']
    price = fmt_price(watch['level'], currency)
    rule = f'{price} 이상이 되면' if watch['type'] == 'breakout' else f'{price} 이하로 내려오면'
    return f'{LABELS[watch["type"]]} · {rule} (무효 {fmt_price(watch["invalidate"], currency)} 이하)'


def clean_fields(report):
    """Keep the director's plan fields only when they are well formed. Returns (fields, note): all-neutral values and a note
    when the plan is unusable, so a bad optional plan never costs the analysis around it."""
    none = {'entry_type': 'none', 'entry_level': 0, 'entry_invalidate': 0, 'entry_minutes': 0}
    kind = report.get('entry_type')
    if kind in (None, 'none'):
        return none, ''
    level, dead, minutes = report.get('entry_level'), report.get('entry_invalidate'), report.get('entry_minutes')
    if (kind not in TYPES or not _number(level) or not _number(dead) or type(minutes) is not int
            or not level > dead > 0 or not 0 < minutes <= 100000):
        return none, '조건 진입 계획의 형식이 올바르지 않아 제외했습니다.'
    return {'entry_type': kind, 'entry_level': float(level), 'entry_invalidate': float(dead), 'entry_minutes': minutes}, ''


def plan_from(decision, *, quote, horizon, now, currency, min_take_pct=0.0):
    """The watch fields for a finished HOLD decision, or (None, why not). `quote` is the price the plan is judged against."""
    kind = decision.get('entry_type')
    if kind not in TYPES:
        return None, ''
    if decision.get('stance') != 'HOLD':
        return None, ''
    level, dead, minutes = decision['entry_level'], decision['entry_invalidate'], decision['entry_minutes']
    ask, bid = quote['ask'], quote['bid']
    weight, stop, take = decision.get('target_weight_pct'), decision.get('stop_loss_pct'), decision.get('take_profit_pct')
    if not (_number(weight) and weight > 0):
        return None, '목표 비중이 0이라 조건 진입을 만들지 않았습니다.'
    if not (_number(stop) and _number(take) and stop > 0 and take >= stop*1.5):
        return None, '손익비(익절 ≥ 손절 × 1.5)를 채우지 못해 조건 진입을 만들지 않았습니다.'
    if take < min_take_pct:
        return None, f'익절 폭 {take:g}%가 최소 {min_take_pct:.2f}%(왕복 비용의 몇 배)에 못 미쳐 조건 진입을 만들지 않았습니다.'
    if kind == 'breakout' and level <= ask:
        return None, '돌파 가격이 이미 현재 호가 이하라 조건 진입을 만들지 않았습니다.'
    if kind == 'pullback' and level >= bid:
        return None, '눌림 가격이 현재 호가 이상이라 조건 진입을 만들지 않았습니다.'
    if (level/ask-1)*100 > MAX_AWAY_PCT[horizon] or (1-level/bid)*100 > MAX_AWAY_PCT[horizon]:
        return None, f'진입 가격이 현재가에서 {MAX_AWAY_PCT[horizon]:g}% 넘게 떨어져 있어 조건 진입을 만들지 않았습니다.'
    if (1-dead/level)*100 > MAX_DEPTH_PCT:
        return None, '무효 가격이 진입 가격에서 너무 멀어 조건 진입을 만들지 않았습니다.'
    if kind == 'breakout' and dead >= bid:
        return None, '무효 가격이 현재 호가 이상이라 조건 진입을 만들지 않았습니다.'
    low, high = MINUTES[horizon]
    minutes = max(low, min(high, minutes))
    expires = now+minutes*60
    if horizon != 'month':
        expires = min(expires, float(quote.get('session_end') or 0)-CLOSE_MARGIN)
        if expires-now < low*60:
            return None, '장 마감이 가까워 조건 진입을 만들지 않았습니다.'
    return {'type': kind, 'level': level, 'invalidate': dead, 'expires': expires,
            'plan': {'target_weight_pct': weight, 'stop_loss_pct': stop, 'take_profit_pct': take,
                     'max_holding_minutes': decision['max_holding_minutes']}}, ''


def make_watch(fields, *, symbol, name, market, currency, horizon, reference, summary, engine, run_id, generation, now):
    return {'id': str(uuid.uuid4()), 'symbol': symbol, 'name': name, 'market': market, 'currency': currency,
            'horizon': horizon, 'status': 'waiting', 'created': now, 'closed': None, 'outcome': '', 'note': '',
            'reference': reference, 'summary': str(summary)[:600], 'engine': str(engine or ''), 'run_id': run_id,
            'generation': generation, 'hits': 0, 'failures': 0, 'last_received': 0, 'last_price': None, 'checked': 0,
            'volume_after': 0, **fields}


def close(watch, status, outcome, now):
    watch.update(status=status, outcome=outcome, closed=now)


def close_waiting(state, status, outcome, now, symbol=None):
    """Close every waiting watch (of one symbol, if given); returns how many were closed."""
    count = 0
    for watch in waiting(state):
        if symbol is None or watch['symbol'] == symbol:
            close(watch, status, outcome, now)
            count += 1
    return count


def trim(state):
    """Waiting watches always stay; of the finished ones only the newest remain."""
    watches = state.get('watches') or []
    drop = {id(w) for w in [w for w in watches if w.get('status') != 'waiting'][:-KEEP]}
    state['watches'] = [w for w in watches if id(w) not in drop]


def evaluate(watch, quote):
    """What the live quote says about a waiting watch: 'invalid' (the idea is dead), 'hit' (buy zone), 'above' (a breakout
    already ran past its zone: no chasing) or 'wait'. The caller has checked that the quote is fresh and tradable."""
    ask, bid = quote['ask'], quote['bid']
    if bid <= watch['invalidate']:
        return 'invalid'
    level = watch['level']
    if watch['type'] == 'breakout':
        if ask > level*(1+CHASE_PCT/100):
            return 'above'
        return 'hit' if ask >= level else 'wait'
    return 'hit' if ask <= level else 'wait'


def volume_ratio(candles):
    """Average volume of the last 5 completed candles over the average of the 20 before them (None when it cannot be told)."""
    volumes = [c['volume'] for c in candles if _number(c.get('volume'))]
    recent, prior = volumes[-5:], volumes[-25:-5]
    if len(recent) < 5 or not prior or sum(prior) <= 0:
        return None
    return (sum(recent)/5)/(sum(prior)/len(prior))


def volume_ok(candles):
    ratio = volume_ratio(candles)
    return ratio is not None and ratio >= VOLUME_RATIO
