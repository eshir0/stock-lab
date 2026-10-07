"""Stock splits: a held position, a waiting conditional entry and the scoring of a name follow a split instead of reading
it as a crash (or a jump).

After a 10:1 split the first quote is a tenth of yesterday's close. Without this module the exit monitor would sell the
position at its stop, a pullback plan would buy, and every score would show -90%. Large names do split (NVDA and AVGO
10:1 in 2024), so:

- `suspicious`: a held or watched name whose price left the band a market allows in one session (Korea has a +-30% daily
  limit; a US large cap moving -40% / +70% in a day is treated as a possible split) is held back: no exit, no entry.
- The ratio is then confirmed from data, never guessed from the price alone: the broker's adjusted daily bars (yesterday's
  close divided by the ratio) or the archive's split record (`splits` in the evidence pack). Confirmed -> `apply`.
  Confirmed "no split" -> the move is real and the rules run again. Unconfirmed for GIVE_UP -> the rules run again too.
- `apply` multiplies the position's shares by the ratio and divides its prices (average, stop, target, high, reference),
  rewrites the open round trip's fills the same way (money amounts unchanged, so the trade's result is unchanged), moves
  waiting plans and records the split, and `factor` lets the scoring divide prices recorded before the split.

Pure functions over the state dict, no I/O.
"""
import math
from datetime import datetime, time as day_time
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import scorecard, shares
from .instruments import SYMBOLS

BANDS = {'KR': (.69, 1.31), 'US': (.6, 1.7)}     # price / reference outside this band in one step: maybe a split
GIVE_UP = 36*3600                               # unconfirmed for this long: treat the move as real
RETRY = 300                                     # look at the bars again at most this often per name
ZONES = {'KR': 'Asia/Seoul', 'US': 'America/New_York'}
PRICE_KEYS = ('average', 'stop_price', 'take_profit_price', 'high_water', 'initial_stop')
WATCH_KEYS = ('level', 'invalidate', 'reference', 'last_price')
# Ratios splits actually use: n-for-1 and 1-for-n (reverse) up to 50, and the few fractional ones (3:2, 5:2, 4:3, 5:4).
CANDIDATES = sorted({float(n) for n in range(2, 51)} | {1/n for n in range(2, 51)} | {1.5, 2.5, 4/3, 1.25, 2/3, .4, .75, .8})


def market(symbol):
    return SYMBOLS[symbol]['market']


def local_date(symbol, when):
    return datetime.fromtimestamp(when, ZoneInfo(ZONES[market(symbol)])).date().isoformat()


def day_start(symbol, date):
    """Unix time of local midnight of `date` (YYYY-MM-DD) in the name's market."""
    y, m, d = map(int, date.split('-'))
    return datetime.combine(datetime(y, m, d).date(), day_time(0), ZoneInfo(ZONES[market(symbol)])).timestamp()


def suspicious(symbol, price, reference):
    if not (isinstance(price, (int, float)) and isinstance(reference, (int, float)) and price > 0 and reference > 0):
        return False
    low, high = BANDS[market(symbol)]
    return not low <= price/reference <= high


def snap(ratio):
    """The split ratio (new shares per old share) `ratio` stands for: the nearest ratio splits actually use (CANDIDATES)
    within 2%, or None when it is near 1 or matches none of them."""
    if not isinstance(ratio, (int, float)) or not math.isfinite(ratio) or ratio <= 0 or abs(ratio-1) < .2:
        return None
    err, best = min((abs(ratio/v-1), v) for v in CANDIDATES)
    return round(best, 6) if err <= .02 else None


def from_bars(reference, bars):
    """1.0 when the adjusted bar of the reference day still shows the reference close (no split), the ratio when it was
    divided by one, None when the bars cannot tell (that day is missing)."""
    if not reference or not reference.get('time'):
        return None
    bar = next((b for b in bars or [] if b.get('time') == reference['time']), None)
    if not bar or not bar.get('close'):
        return None
    ratio = reference['close']/bar['close']
    if abs(ratio-1) < .03:
        return 1.0
    return snap(ratio)


def from_pack(pack, symbol, after):
    """The ratio of a split in the archive record with an ex-date after `after` (unix time), or None."""
    found = [s for s in (pack or {}).get('splits') or []
             if isinstance(s.get('ratio'), (int, float)) and isinstance(s.get('ex_date'), str) and day_start(symbol, s['ex_date']) > after]
    return snap(found[-1]['ratio']) if found else None


def known(state, symbol, date):
    return any(x.get('date') == date for x in (state.get('splits') or {}).get(symbol, []))


def factor(state, symbol, since):
    """How much a price recorded at `since` must be divided by to compare with today's adjusted prices."""
    f = 1.0
    for x in (state.get('splits') or {}).get(symbol, []):
        if x.get('time', 0) > since:
            f *= x['ratio']
    return f


def _scale_quantity(quantity, ratio, fractional):
    return shares.number(shares.floor_to(Decimal(str(quantity))*Decimal(str(ratio)), fractional))


def apply(state, symbol, ratio, at, source, fractional):
    """Adjust everything recorded before `at` (the split's time) for a split of `ratio`. Returns the event text, or None
    when this split is already recorded."""
    date = local_date(symbol, at)
    if known(state, symbol, date):
        return None
    pos = state.get('positions', {}).get(symbol)
    note = ''
    opened = (pos or {}).get('opened') or next((t['opened'] for t in scorecard.round_trips(state.get('trades') or [])[1]
                                                 if t['symbol'] == symbol), 0)
    if pos and opened < at:
        before = pos['quantity']
        pos['quantity'] = _scale_quantity(before, ratio, fractional)
        exact = float(Decimal(str(before))*Decimal(str(ratio)))
        fraction = round(exact-pos['quantity'], 6)
        if fraction > 0 and pos.get('average'):
            # Whole shares only: the broker pays the fraction in cash at the adjusted average, so the trade's result is
            # unchanged; a sell of the fraction keeps the ledger's share count equal to the position's.
            price = pos['average']/ratio
            cash = round(fraction*price, 2)
            currency = SYMBOLS[symbol]['currency']
            state['cash'][currency] = round(state['cash'][currency]+cash, 2)
            if isinstance(pos.get('cost_basis'), (int, float)):
                pos['cost_basis'] = round(pos['cost_basis']-cash, 2)
            state.setdefault('trades', []).append({'id': f'split-{symbol}-{local_date(symbol, at)}', 'time': at, 'symbol': symbol,
                                                   'side': 'SELL', 'quantity': fraction, 'price': round(price, 6), 'fee': 0,
                                                   'currency': currency, 'realized': 0, 'split_cash': True,
                                                   'exit_reason': '분할 단주 정산'})
            note = f' · 1주 미만 {fraction:g}주는 {cash:,.2f} {currency} 현금 정산'
        for key in PRICE_KEYS:
            if isinstance(pos.get(key), (int, float)):
                pos[key] = round(pos[key]/ratio, 6)
        if isinstance(pos.get('ref_close'), dict) and pos['ref_close'].get('close'):
            pos['ref_close'] = dict(pos['ref_close'], close=pos['ref_close']['close']/ratio)
        pos.setdefault('splits', []).append({'date': date, 'ratio': ratio})
        for t in state.get('trades') or []:
            if t.get('symbol') == symbol and opened <= t.get('time', 0) < at and not t.get('split_cash'):
                t['quantity'] = _scale_quantity(t['quantity'], ratio, True)
                t['price'] = round(t['price']/ratio, 6)
                t['split_ratio'] = round(t.get('split_ratio', 1)*ratio, 6)
        note = f' · 보유 {before:g}주 → {pos["quantity"]:g}주'+note
    for w in state.get('watches') or []:
        if w.get('symbol') == symbol and w.get('status') == 'waiting' and w.get('created', 0) < at:
            for key in WATCH_KEYS:
                if isinstance(w.get(key), (int, float)):
                    w[key] = round(w[key]/ratio, 6)
    for p in state.get('proposals') or []:
        if p.get('symbol') == symbol and p.get('status') == 'pending':
            p['status'] = 'invalidated'
    state.setdefault('splits', {}).setdefault(symbol, []).append({'date': date, 'ratio': ratio, 'time': at, 'source': source})
    state.get('split_checks', {}).pop(symbol, None)
    shown = f'{ratio:g}:1' if ratio >= 1 else f'1:{1/ratio:g}'
    return f'{SYMBOLS[symbol]["name"]} 주식 분할({shown}, {date})을 반영했습니다{note}. 가격·손절·조건 진입·채점 기준을 같은 비율로 맞췄습니다.'
