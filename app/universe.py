"""Daily focus list: which names the intraday desk may buy today. Pure functions, no I/O, paper-only.

The list is built each morning in three steps, and every guard below is enforced by code, not by a prompt:

1. **Data screen.** Each pool name is measured on completed daily candles (one-month trend, distance from the
   20-day average, five-day run-up, average true range, traded value). Names that are in a downtrend, already
   stretched, recently spiked, too quiet, too wild or too thinly traded are excluded with a written reason.
2. **News and theme read.** The AI may re-order and veto ONLY the names that passed step 1. It cannot add a symbol,
   its picks are ignored unless it cited retrieved sources, and a pick it labels "already priced in" is dropped.
3. **Fill.** If the AI is unavailable or gives fewer names than wanted, the best remaining names by data score fill
   the list. With no AI at all the list is purely data-driven.

Nothing here can place or size an order. A past month's winners do not promise a future month's profit, so every
day's picks are stored with their reference prices and later scored against the whole pool (`outcome`).
"""
import math

LOOKBACK = 21                   # completed sessions, about one trading month
MIN_CANDLES = 26                # SMA20 + the one-month return + slack
MAX_CANDLE_AGE = 8*86400        # newest completed candle may be older than this only across holidays
LEAD_SECONDS = 90*60            # the list is built this long before the regular session opens
MAX_AGE = 30*3600               # a list older than this is ignored (falls back to the fixed lineup)
TOP_FOR_AI = 8                  # data-screen survivors shown to the AI
ROTATION_WARMUP = 20*60         # a rotation sell waits until this long after the open (no opening-noise exits)
MAX_DAILY_MOVE = 45.0           # % — a larger one-day move in adjusted candles is treated as a data problem
MIN_TURNOVER = {'KRW': 50e9, 'USD': 300e6}   # average traded value over 20 sessions, in local currency
CAPS = {  # ext: % above the 20-day average; run5: five-day gain; atr: allowed average-true-range band (% of price)
    'stock': {'ext': 12.0, 'run5': 18.0, 'atr': (0.8, 6.0)},
    'etf': {'ext': 10.0, 'run5': 14.0, 'atr': (0.5, 5.0)},
    'lev': {'ext': 25.0, 'run5': 35.0, 'atr': (1.5, 12.0)}}
RISK_ORDER = {'low': 0, 'medium': 1}
WEIGHTS = {'trend': .35, 'consistency': .15, 'tradability': .20, 'liquidity': .15, 'attention': .15}
KIND_LABELS = {'stock': '주식', 'etf': 'ETF', 'lev': '레버리지·인버스 ETF'}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _pct(new, old):
    return (new/old-1)*100 if old else 0.0


def _clamp(value, low, high):
    return max(low, min(high, value))


def kind_of(item):
    if item.get('leveraged_etf'):
        return 'lev'
    return 'etf' if item.get('etf') else 'stock'


def daily_metrics(candles, now=None):
    """Facts computed from completed daily candles, or None when the data cannot support them.

    Only candles marked completed are used, so a day still in progress never leaks into the measurement.
    """
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
    if len(rows) < MIN_CANDLES:
        return None
    if now is not None and now-rows[-1]['time'] > MAX_CANDLE_AGE:
        return None
    closes = [c['close'] for c in rows]
    last = closes[-1]
    sma20 = sum(closes[-20:])/20
    moves = [_pct(b, a) for a, b in zip(closes[-21:], closes[-20:])]
    true_ranges = []
    for prev, c in zip(rows[-15:-1], rows[-14:]):
        high = c['high'] if _finite(c.get('high')) else c['close']
        low = c['low'] if _finite(c.get('low')) else c['close']
        true_ranges.append(max(high-low, abs(high-prev['close']), abs(low-prev['close'])))
    return {'last': round(last, 4), 'asof': rows[-1]['time'], 'sma20': round(sma20, 4),
            'ret_1m_pct': round(_pct(last, closes[-1-LOOKBACK]), 2), 'ret_5d_pct': round(_pct(last, closes[-6]), 2),
            'ext_20d_pct': round(_pct(last, sma20), 2), 'atr_pct': round(sum(true_ranges)/len(true_ranges)/last*100, 2),
            'up_ratio': round(sum(1 for m in moves if m > 0)/len(moves), 3),
            'turnover': round(sum(c['close']*c['volume'] for c in rows[-20:])/20),
            'max_move_pct': round(max(abs(m) for m in moves), 2), 'sessions': len(rows)}


def screen(m, item, relaxed=False):
    """Hard filters. An empty list means the name may be considered; otherwise the reasons are shown to the owner.

    `relaxed` skips only the traded-value floor (demo candles carry synthetic volume).
    """
    caps = CAPS[kind_of(item)]
    reasons = []
    if m['last'] <= m['sma20']:
        reasons.append('20일선 아래 · 하락 추세')
    if m['ret_1m_pct'] <= 0:
        reasons.append(f'최근 1개월 수익률 {m["ret_1m_pct"]:+.1f}% · 상승 추세 아님')
    if m['ext_20d_pct'] > caps['ext']:
        reasons.append(f'20일선보다 {m["ext_20d_pct"]:+.1f}% 높음 · 이미 오른 뒤일 수 있어 추격 금지')
    if m['ret_5d_pct'] > caps['run5']:
        reasons.append(f'최근 5일 {m["ret_5d_pct"]:+.1f}% 급등 · 추격 금지')
    low, high = caps['atr']
    if not low <= m['atr_pct'] <= high:
        reasons.append(f'하루 변동폭 {m["atr_pct"]:.1f}%가 허용 범위({low:g}~{high:g}%) 밖')
    if m['max_move_pct'] > MAX_DAILY_MOVE:
        reasons.append(f'일봉에 {m["max_move_pct"]:.0f}% 급변이 있어 데이터 확인 필요')
    if not relaxed and m['turnover'] < MIN_TURNOVER.get(item['currency'], math.inf):
        reasons.append('평균 거래대금 부족')
    return reasons


def _tradability(atr, kind):
    low, high = CAPS[kind]['atr']
    ideal_low, ideal_high = low*1.9, high*.6
    if atr < ideal_low:
        return _clamp((atr-low)/(ideal_low-low), 0, 1)*100
    if atr <= ideal_high:
        return 100.0
    return _clamp(1-(atr-ideal_high)/(high-ideal_high), 0, 1)*70+30


def _attention(attention, symbol):
    """Points for how much market attention the name had. None (no ranking data at all) is neutral."""
    if attention is None:
        return 50.0
    entry = attention.get(symbol) or {}
    ranks = [entry[k] for k in ('amount', 'volume') if isinstance(entry.get(k), int)]
    if not ranks:
        return 20.0
    best = min(ranks)
    return 100.0 if best <= 10 else 75.0 if best <= 30 else 45.0


def score(m, item, attention=None, relaxed=False):
    """0-100 score with its parts, so the owner can see why a name ranks where it does."""
    kind = kind_of(item)
    caps = CAPS[kind]
    floor = MIN_TURNOVER.get(item['currency'], 1)
    parts = {
        'trend': _clamp(m['ret_1m_pct']/15, 0, 1)*100,
        'consistency': _clamp((m['up_ratio']-.4)/.3, 0, 1)*100,
        'tradability': _tradability(m['atr_pct'], kind),
        'liquidity': 50.0 if relaxed else _clamp(math.log10(max(m['turnover'], 1)/floor), 0, 1)*100,
        'attention': _attention(attention, item['symbol'])}
    half = caps['ext']/2
    penalty = _clamp((m['ext_20d_pct']-half)/half, 0, 1)*25
    total = sum(WEIGHTS[k]*v for k, v in parts.items())-penalty
    parts['extension_penalty'] = round(penalty, 1)
    return round(_clamp(total, 0, 100), 1), {k: round(v, 1) for k, v in parts.items()}


def rank_pool(items, metrics, attention=None, relaxed=False):
    """-> (passed sorted by score, excluded with reasons). `metrics` maps symbol -> daily_metrics or None."""
    passed, excluded = [], []
    for item in items:
        m = metrics.get(item['symbol'])
        if m is None:
            excluded.append({'symbol': item['symbol'], 'name': item['name'], 'reasons': ['일봉 데이터 부족']})
            continue
        reasons = screen(m, item, relaxed)
        if reasons:
            excluded.append({'symbol': item['symbol'], 'name': item['name'], 'reasons': reasons})
            continue
        total, parts = score(m, item, attention, relaxed)
        entry = attention.get(item['symbol']) if attention is not None else None
        passed.append({'symbol': item['symbol'], 'name': item['name'], 'kind': kind_of(item), 'currency': item['currency'],
                       'leverage_factor': item.get('leverage_factor', 1), 'underlying': item.get('underlying', ''),
                       'score': total, 'parts': parts,
                       'rank_amount': (entry or {}).get('amount'), 'rank_volume': (entry or {}).get('volume'),
                       **{k: m[k] for k in ('last', 'ret_1m_pct', 'ret_5d_pct', 'ext_20d_pct', 'atr_pct', 'up_ratio', 'turnover')}})
    passed.sort(key=lambda p: (-p['score'], p['symbol']))
    return passed, excluded


def ai_candidates(passed, limit=TOP_FOR_AI):
    """What the news/theme step is shown: server-computed numbers only, and only names that passed the screen."""
    keys = ('symbol', 'name', 'kind', 'leverage_factor', 'underlying', 'score', 'ret_1m_pct', 'ret_5d_pct', 'ext_20d_pct',
            'atr_pct', 'rank_amount', 'rank_volume')
    return [{k: p.get(k) for k in keys} for p in passed[:limit]]


def choose(passed, ai, n):
    """Merge the AI's read into the data ranking -> (picks, notes).

    - Only symbols in `passed` can ever be picked; anything else the AI said is ignored.
    - An AI read without retrieved sources (`grounded` false) is not used at all.
    - A pick the AI marks as already priced in (`high`) is dropped; a symbol on its avoid list is never filled in.
    - AI picks come first (lower risk before medium, the AI's own order otherwise), then the best data scores.
    """
    offered = {p['symbol']: p for p in passed}
    picks, notes, taken = [], [], set()
    grounded = bool(ai and ai.get('grounded'))
    avoid = {a['symbol'] for a in (ai or {}).get('avoid', []) if isinstance(a, dict)} if grounded else set()
    if grounded:
        # A name the AI calls already priced in is not "filled back in" by the data ranking either.
        avoid |= {p.get('symbol') for p in ai.get('picks', []) if isinstance(p, dict) and p.get('priced_in_risk') == 'high'}
    if ai and not grounded:
        notes.append('AI가 검색 출처를 제시하지 않아 뉴스 판단은 반영하지 않았습니다.')
    if grounded:
        ordered = sorted((p for p in ai.get('picks', []) if isinstance(p, dict)),
                         key=lambda p: RISK_ORDER.get(p.get('priced_in_risk'), 2))
        for pick in ordered:
            symbol = pick.get('symbol')
            if symbol not in offered or symbol in taken:
                continue
            if pick.get('priced_in_risk') not in RISK_ORDER:
                notes.append(f'{symbol}: 이미 가격에 반영됐을 위험이 커서 제외했습니다.')
                continue
            if len(picks) < n:
                picks.append({**offered[symbol], 'source': 'ai', 'ai': {k: pick.get(k) for k in
                                                                       ('theme', 'catalyst', 'priced_in_risk', 'reason')}})
                taken.add(symbol)
    for p in passed:
        if len(picks) >= n:
            break
        if p['symbol'] in taken or p['symbol'] in avoid:
            continue
        picks.append({**p, 'source': 'data', 'ai': None})
        taken.add(p['symbol'])
    return picks, notes


def rotation_decision(position, m, price, avoid=False):
    """A held name that is not in today's list: keep it running, or sell it — never add to it.

    Trend intact (above its 20-day average, no 5-day slide of more than 3%, not on the AI's avoid list):
    keep, and let the stop, target, holding-time and closing rules manage it, so a mere list change does not sell a
    winner early or lock in a loss. Trend broken: sell (locks a gain, or cuts a loser before it grows).
    """
    pnl = _pct(price, position['average']) if _finite(price) and position.get('average') else None
    if m is None:
        return {'action': 'keep', 'pnl_pct': _round(pnl), 'trend_ok': None,
                'reason': '일봉 데이터가 없어 손절·익절·보유시간 규칙에만 맡깁니다.'}
    trend_ok = m['last'] > m['sma20'] and m['ret_5d_pct'] > -3 and not avoid
    if trend_ok:
        return {'action': 'keep', 'pnl_pct': _round(pnl), 'trend_ok': True,
                'reason': '추세가 유지돼 강제로 팔지 않고 손절·익절·보유시간 규칙에 맡깁니다. 추가 매수는 하지 않습니다.'}
    why = 'AI 뉴스 검토에서 위험 종목으로 분류됐고' if avoid else '추세가 꺾였고'
    outcome = '수익을 지키려고' if pnl is not None and pnl > 0 else '손실이 커지기 전에'
    return {'action': 'sell', 'pnl_pct': _round(pnl), 'trend_ok': False,
            'reason': f'{why} 집중 종목에서도 빠져 {outcome} 개장 후 매도합니다.'}


def _round(value):
    return round(value, 2) if value is not None else None


def outcome(record, series):
    """Close-to-close result of one day's picks versus the whole pool and the fixed lineup, once the next completed
    candle after the reference candle exists. `series` maps symbol -> completed daily candles.
    Returns None while the outcome cannot be measured yet."""
    def follow(symbol):
        ref = (record.get('ref') or {}).get(symbol)          # [time, close] of the last completed candle at selection
        rows = sorted((c for c in series.get(symbol) or [] if c.get('completed') and _finite(c.get('close')) and c['close'] > 0),
                      key=lambda c: c['time'])
        if not ref or not rows:
            return None
        after = [c for c in rows if c['time'] > ref[0]]
        return _pct(after[0]['close'], ref[1]) if after else None

    def average(symbols):
        values = [v for v in (follow(s) for s in symbols) if v is not None]
        return (sum(values)/len(values), len(values)) if values else (None, 0)
    picks, n_picks = average(record.get('picks', []))
    pool, n_pool = average(list((record.get('ref') or {})))
    fixed, n_fixed = average(record.get('fixed', []))
    if picks is None or pool is None:
        return None
    return {'pick_pct': round(picks, 2), 'pool_pct': round(pool, 2), 'fixed_pct': _round(fixed),
            'excess_pool_pct': round(picks-pool, 2), 'excess_fixed_pct': _round(picks-fixed if fixed is not None else None),
            'n_picks': n_picks, 'n_pool': n_pool}


def summarize(history):
    """Average over the measured days, per market and overall. Small samples say nothing; the count is always shown."""
    def block(records):
        done = [r['result'] for r in records if r.get('result')]
        if not done:
            return {'days': 0}
        excess = [r['excess_pool_pct'] for r in done]
        fixed = [r['excess_fixed_pct'] for r in done if r.get('excess_fixed_pct') is not None]
        return {'days': len(done), 'pick_pct': round(sum(r['pick_pct'] for r in done)/len(done), 2),
                'pool_pct': round(sum(r['pool_pct'] for r in done)/len(done), 2),
                'excess_pool_pct': round(sum(excess)/len(excess), 2),
                'excess_fixed_pct': round(sum(fixed)/len(fixed), 2) if fixed else None,
                'beat_pool_days': sum(1 for e in excess if e > 0)}
    return {'all': block(history), **{m: block([r for r in history if r.get('market') == m]) for m in ('KR', 'US')}}
