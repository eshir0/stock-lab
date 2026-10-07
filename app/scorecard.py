"""Report card of the trades themselves, a market benchmark to compare with, and the statistics the verification plan uses.

A trade is judged from its own fills: a round trip runs from the first buy of a name (nothing held) to the sell that leaves
nothing held, and its result is what the ledger really booked - every fee and tax included. `report` turns the round trips
into the numbers a decision about real money needs: how often it wins, by how much, the expectancy and how sure we can be
of it. `benchmark_entry` follows an index ETF bought at the start of the experiment and simply held.

Pure functions over plain dicts; nothing here trades or calls a service.
"""
import math
import time
from datetime import datetime, timezone
from decimal import Decimal

from . import hours, shares
from .instruments import SYMBOLS

BENCHMARKS = {'KRW': '069500', 'USD': 'SPY'}     # KODEX 200 and SPDR S&P 500: what simply holding the market earned
# Two-sided 95% Student-t critical values by degrees of freedom. Between rows the smaller df (the wider interval) is used,
# so a small sample is never treated as surer than it is; beyond the table the normal value applies.
T95 = ((1, 12.706), (2, 4.303), (3, 3.182), (4, 2.776), (5, 2.571), (6, 2.447), (7, 2.365), (8, 2.306), (9, 2.262),
       (10, 2.228), (12, 2.179), (15, 2.131), (20, 2.086), (25, 2.060), (30, 2.042), (40, 2.021), (60, 2.000), (120, 1.980))
RECENT = 20                                      # closed round trips listed on the dashboard


def t95(df):
    if df < 1:
        return None
    if df > 120:
        return 1.96
    return next(t for key, t in reversed(T95) if df >= key)


def mean_ci(values):
    """(mean, low, high): the mean and a 95% confidence interval for it. low/high are None below two values."""
    values = [float(v) for v in values]
    n = len(values)
    if not n:
        return None, None, None
    mean = sum(values)/n
    if n < 2:
        return round(mean, 4), None, None
    sd = math.sqrt(sum((v-mean)**2 for v in values)/(n-1))
    half = t95(n-1)*sd/math.sqrt(n)
    return round(mean, 4), round(mean-half, 4), round(mean+half, 4)


def _origin(trade):
    origin = trade.get('origin')
    if origin in ('watch', 'ai'):
        return origin
    return 'watch' if trade.get('entry_watch') else 'ai'


def _close(trip, when):
    invested, pnl = trip['invested'], trip['pnl']
    return {'symbol': trip['symbol'], 'name': trip['name'], 'currency': trip['currency'], 'opened': trip['opened'],
            'closed': when, 'days': round((when-trip['opened'])/86400, 2), 'pnl': round(float(pnl), 2),
            'return_pct': round(float(pnl/invested*100), 4) if invested > 0 else 0.0, 'leveraged': trip['leveraged'],
            'origin': trip['origin'], 'reused': trip['reused'], 'exit_reason': trip.get('exit_reason', ''),
            'evidence': trip.get('evidence')}


def round_trips(trades, dividends=()):
    """(closed round trips, open ones), oldest first, built from the ledger's fills. A sell without a recorded buy is ignored."""
    holding, closed = {}, []
    events = [dict(t) for t in trades]+[dict(d, side='DIVIDEND') for d in dividends or ()]
    for t in sorted(events, key=lambda x: (x.get('time', 0), x.get('side') == 'DIVIDEND')):
        symbol, side = t.get('symbol'), t.get('side')
        if side == 'DIVIDEND':
            if symbol in holding:
                holding[symbol]['pnl'] += Decimal(str(t.get('net') or 0))     # cash after withholding, part of this trade's result
            continue
        if symbol not in SYMBOLS or side not in ('BUY', 'SELL'):
            continue
        qty = shares.dec(t['quantity'])
        trip = holding.get(symbol)
        if side == 'BUY':
            if trip is None:
                item = SYMBOLS[symbol]
                trip = holding[symbol] = {'symbol': symbol, 'name': item['name'], 'currency': item['currency'],
                                          'opened': t['time'], 'held': Decimal(0), 'invested': Decimal(0), 'pnl': Decimal(0),
                                          'leveraged': bool(item.get('leveraged_etf')), 'origin': _origin(t),
                                          'reused': bool(t.get('reused')), 'evidence': t.get('evidence')}
            trip['held'] += qty
            trip['invested'] += Decimal(str(t['price']))*qty+Decimal(str(t.get('fee') or 0))
        elif trip is not None:
            trip['held'] -= qty
            trip['pnl'] += Decimal(str(t.get('realized') or 0))
            trip['exit_reason'] = t.get('exit_reason') or ('전량 매도' if t.get('liquidation') else '')
            if trip['held'] <= 0:
                closed.append(_close(trip, t['time']))
                del holding[symbol]
    return closed, list(holding.values())


def _avg(values):
    return round(sum(values)/len(values), 4) if values else None


def group(trips):
    """Count, win rate, expectancy and its confidence interval of some round trips."""
    returns = [t['return_pct'] for t in trips]
    mean, low, high = mean_ci(returns)
    return {'count': len(trips), 'win_rate_pct': round(sum(r > 0 for r in returns)/len(returns)*100, 1) if returns else None,
            'expectancy_pct': mean, 'ci_pct': [low, high] if low is not None else None}


US_TAX_RATE, US_TAX_ALLOWANCE_KRW = .22, 2_500_000     # Korea: overseas stock gains 22% (incl. local tax) above 2.5M KRW a year


def us_capital_gains_tax(trades, usdkrw, now):
    """What this year's realized US gains would cost in Korean tax if they were real: (gains in KRW - 2.5M) x 22%. An estimate
    for the owner only - the real figure nets every overseas trade of the year and uses each sale's exchange rate."""
    year = time.strftime('%Y', time.localtime(now))
    gains = sum(float(t.get('realized') or 0) for t in trades
                if t.get('side') == 'SELL' and t.get('currency') == 'USD' and time.strftime('%Y', time.localtime(t.get('time', 0))) == year)
    if not usdkrw:
        return {'year': year, 'realized_usd': round(gains, 2), 'usdkrw': None, 'tax_krw': None}
    krw = gains*usdkrw
    return {'year': year, 'realized_usd': round(gains, 2), 'usdkrw': round(usdkrw, 2), 'realized_krw': round(krw),
            'tax_krw': round(max(0.0, krw-US_TAX_ALLOWANCE_KRW)*US_TAX_RATE)}


def dividend_summary(dividends):
    out = {}
    for d in dividends or []:
        row = out.setdefault(d['currency'], {'count': 0, 'gross': 0.0, 'tax': 0.0, 'net': 0.0})
        row['count'] += 1
        for k in ('gross', 'tax', 'net'):
            row[k] = round(row[k]+float(d[k]), 2)
    return out


def report(trades, dividends=()):
    """The trade report card. Returns are per round trip in percent of what was invested (fees and taxes included); a round
    trip that ends exactly even counts as a loss."""
    closed, holding = round_trips(trades, dividends)
    returns = [t['return_pct'] for t in closed]
    wins, losses = [r for r in returns if r > 0], [r for r in returns if r <= 0]
    base = group(closed)
    avg_win, avg_loss = _avg(wins), _avg(losses)
    lost = abs(sum(losses))
    pnl = {}
    for t in closed:
        pnl[t['currency']] = round(pnl.get(t['currency'], 0.0)+t['pnl'], 2)
    return {'closed': len(closed), 'open': len(holding), 'wins': len(wins), 'losses': len(losses),
            'win_rate_pct': base['win_rate_pct'], 'avg_win_pct': avg_win, 'avg_loss_pct': avg_loss,
            'payoff': round(avg_win/abs(avg_loss), 3) if avg_win is not None and avg_loss else None,
            'profit_factor': round(sum(wins)/lost, 3) if lost > 0 else None,
            'expectancy_pct': base['expectancy_pct'], 'ci_pct': base['ci_pct'],
            'avg_days': _avg([t['days'] for t in closed]), 'best_pct': max(returns) if returns else None,
            'worst_pct': min(returns) if returns else None, 'pnl': pnl,
            'groups': {'leveraged': group([t for t in closed if t['leveraged']]),
                       'plain': group([t for t in closed if not t['leveraged']]),
                       'watch': group([t for t in closed if t['origin'] == 'watch']),
                       'analysis': group([t for t in closed if t['origin'] != 'watch']),
                       'reused': group([t for t in closed if t['reused']]),
                       'fresh': group([t for t in closed if not t['reused']]),
                       'evidence_for': group([t for t in closed if t.get('evidence') == 'for']),
                       'evidence_against': group([t for t in closed if t.get('evidence') == 'against']),
                       'evidence_mixed': group([t for t in closed if t.get('evidence') in ('mixed', 'none')])},
            'recent': closed[-RECENT:][::-1], 'gaps': gap_summary(trades)}


def gap_summary(trades):
    """Stop exits that a gap pushed below the stop (the market opened there): how many and how far past the stop they
    filled. A resting stop order at a broker fills at the same opening price, so this is the cost of holding overnight."""
    gaps = [t['gap_pct'] for t in trades if t.get('side') == 'SELL' and isinstance(t.get('gap_pct'), (int, float))]
    return {'count': len(gaps), 'avg_pct': _avg(gaps), 'worst_pct': min(gaps) if gaps else None}


def _price_entry(symbol, bars, started_at, previous=None):
    """An index ETF bought at the last close that was already set when the experiment started, and simply held: its return
    and worst drawdown to the latest completed close. None until bars exist. Once set, the starting close only moves in one
    case: the provider marks a daily bar complete a day after its date, so an experiment started after the close but
    before midnight first sees the previous session; when the start day's own bar arrives it becomes the start.

    The fetch only covers the last few months, so the highest close and the worst drawdown are carried in the entry and
    only bars newer than the last one processed are added: an empty answer, or a window that no longer reaches back to an
    early peak, never erases what was already seen."""
    bars = sorted((b for b in bars or [] if isinstance(b.get('close'), (int, float)) and b['close'] > 0), key=lambda b: b['time'])
    market = SYMBOLS[symbol]['market']

    def before_start(bar):           # its closing price was already set when the experiment started
        day = datetime.fromtimestamp(bar['time']+43200, timezone.utc).date()
        return bar['time'] <= started_at and hours.closing_time(market, day) <= started_at
    if previous and any(before_start(b) and b['time'] > previous.get('start_time', 0) for b in bars):
        previous = None              # a daily bar is marked complete only a day later: a session that had already closed
                                     # when the experiment started arrived after the start was fixed, so it becomes the start
    if previous and previous.get('symbol') == symbol and previous.get('start_close'):
        start_time, start = previous['start_time'], previous['start_close']
        seen = previous.get('last_time', start_time)
        if 'peak' in previous:
            peak, worst = previous['peak'], previous.get('max_drawdown_pct', 0.0)
        else:                          # an entry saved before the peak was kept: rebuild it from what is still visible
            peak, worst = start, previous.get('max_drawdown_pct', 0.0)
            for b in bars:
                if start_time < b['time'] <= seen:
                    peak = max(peak, b['close'])
        new = [b for b in bars if b['time'] > seen]
        if not new:
            return dict(previous, peak=peak)
        last_time, last_close = previous.get('last_time', start_time), previous.get('last_close', start)
    else:
        before = [b for b in bars if before_start(b)]
        if not before:
            return previous
        start_time, start = before[-1]['time'], before[-1]['close']
        peak, worst = start, 0.0
        new = [b for b in bars if b['time'] > start_time]
        last_time, last_close = start_time, start
    for b in new:
        peak = max(peak, b['close'])
        worst = max(worst, (peak-b['close'])/peak*100)
        last_time, last_close = b['time'], b['close']
    return {'symbol': symbol, 'name': SYMBOLS[symbol]['name'], 'start_time': start_time, 'start_close': start,
            'last_time': last_time, 'last_close': last_close, 'return_pct': round((last_close/start-1)*100, 3),
            'max_drawdown_pct': round(worst, 3), 'peak': peak}


def _bar_day(timestamp):
    return datetime.fromtimestamp(timestamp+43200, timezone.utc).date().isoformat()


def benchmark_entry(symbol, bars, started_at, previous=None, dividends=None, withholding=0.0):
    """`_price_entry` plus the index ETF's own dividends, like the experiment's positions get theirs (engine.credit_dividends):
    every ex-date after the starting session and up to the latest close adds the cash per share after `withholding`, and
    `return_pct` is the total return. Without that the index would be measured on price alone while the account it is
    compared with collects dividends. Paid dividends are carried in the entry, so a pack that is stale for a day removes
    nothing. The drawdown stays on price (the dividend arrives as cash, it does not lift the closes)."""
    entry = _price_entry(symbol, bars, started_at, previous)
    if not entry:
        return entry
    first, last = _bar_day(entry['start_time']), _bar_day(entry['last_time'])
    paid = {ex: net for ex, net in ((previous or {}).get('dividends') or {}).items()
            if (previous or {}).get('start_time') == entry['start_time']}
    for d in dividends or []:
        ex, amount = d.get('ex_date'), d.get('amount')
        if isinstance(ex, str) and isinstance(amount, (int, float)) and amount > 0:
            paid.setdefault(ex, round(amount*(1-withholding), 6))
    paid = {ex: net for ex, net in sorted(paid.items()) if first < ex <= last}
    per_share = sum(paid.values())
    return dict(entry, dividends=paid, dividend_per_share=round(per_share, 6), price_return_pct=entry['return_pct'],
                return_pct=round((entry['last_close']+per_share)/entry['start_close']*100-100, 3))
