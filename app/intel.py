"""Compact market context from Toss's official, read-only ranking and investor-flow endpoints.

Everything here is an *input for the AI's stock selection and analysis*. Nothing in this module can
place, size or approve an order, and a failed or malformed response only removes the extra context.
"""
import math
import threading
import time

# (key, Toss ranking type, duration). TOP_GAINERS/TOP_LOSERS do not support "realtime".
RANK_KINDS = (('volume', 'MARKET_TRADING_VOLUME', 'realtime'),
              ('amount', 'MARKET_TRADING_AMOUNT', 'realtime'),
              ('gainers', 'TOP_GAINERS', '1d'),
              ('losers', 'TOP_LOSERS', '1d'))
RANK_TTL = 60         # seconds between ranking refreshes while a market is open
RANK_RETRY = 15       # seconds before a failed or missing ranking is tried again
FLOW_TTL = 300        # investor flows are daily figures; refresh every five minutes
MAX_AGE = 600         # rankings older than this are not shown to the AI as current
FLOW_DAYS = 5
FLOW_MAX_AGE = 86400  # daily figures stay useful, but always carry their own age
OUTSIDE = '100위 밖'


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value):
    number = _number(value)
    return int(number) if number is not None else None


def parse_rankings(result):
    """{'items': {symbol: {'rank': int, 'change_pct': float|None}}} or None when unusable."""
    rows = result.get('rankings') if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return None
    items = {}
    for row in rows[:100]:
        if not isinstance(row, dict) or not isinstance(row.get('symbol'), str):
            continue
        rank = _integer(row.get('rank'))
        if rank is None or rank < 1:
            continue
        price = row.get('price') if isinstance(row.get('price'), dict) else {}
        change = _number(price.get('changeRate'))
        items[row['symbol']] = {'rank': rank, 'change_pct': round(change*100, 3) if change is not None else None}
    return {'items': items}


def _side(record, key):
    part = record.get(key)
    return _integer(part.get('netBuyVolume')) if isinstance(part, dict) else None


def parse_investor_trading(result, today=None):
    """Recent daily net-buy volumes (shares) for a Korean stock, newest first."""
    rows = result.get('records') if isinstance(result, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    latest = rows[0]
    recent = [r for r in rows[:FLOW_DAYS] if isinstance(r, dict)]

    def total(key):
        values = [_side(r, key) for r in recent]
        values = [v for v in values if v is not None]
        return sum(values) if values else None
    date = latest.get('date') if isinstance(latest.get('date'), str) else None
    return {'date': date, 'provisional': bool(today and date == today),
            'foreigner_net_shares': _side(latest, 'foreigner'),
            'institution_net_shares': _side(latest, 'institution'),
            'individual_net_shares': _side(latest, 'individual'),
            'foreigner_5d_net_shares': total('foreigner'),
            'institution_5d_net_shares': total('institution'),
            'days': len(recent)}


class MarketIntel:
    """Thread-safe cache: the refresh loop writes, the analysis cycle reads."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.ranks = {}       # market -> {'at': float, 'kinds': {key: {symbol: {...}}}}
        self.flows = {}       # symbol -> {'at': float, 'data': {...}}
        self.errors = {}      # (market, kind) -> last failure label, cleared on success
        self.attempts = {}    # market -> time of the last ranking fetch attempt

    def rank_age(self, market):
        with self.lock:
            entry = self.ranks.get(market)
        return self.clock()-entry['at'] if entry else None

    def flow_age(self, symbol):
        with self.lock:
            entry = self.flows.get(symbol)
        return self.clock()-entry['at'] if entry else None

    def note_error(self, market, kind, message):
        with self.lock:
            if message:
                self.errors[f'{market}:{kind}'] = str(message)[:200]
            else:
                self.errors.pop(f'{market}:{kind}', None)

    def attempt_age(self, market):
        with self.lock:
            at = self.attempts.get(market)
        return self.clock()-at if at is not None else None

    def mark_attempt(self, market):
        with self.lock:
            self.attempts[market] = self.clock()

    def kinds(self, market):
        with self.lock:
            entry = self.ranks.get(market)
            return set(entry['kinds']) if entry else set()

    def store_rankings(self, market, kinds, merge=False):
        """A full refresh replaces the snapshot; a retry of missing lists is merged into it."""
        with self.lock:
            entry = self.ranks.get(market) if merge else None
            merged = dict(entry['kinds'], **kinds) if entry else dict(kinds)
            self.ranks[market] = {'at': self.clock(), 'kinds': merged}

    def store_flow(self, symbol, data):
        with self.lock:
            self.flows[symbol] = {'at': self.clock(), 'data': data}

    def status(self):
        """Freshness summary for the dashboard: what was fetched and how old it is."""
        now = self.clock()
        with self.lock:
            rankings = {market: {'age': int(now-entry['at']), 'kinds': sorted(entry['kinds'])}
                        for market, entry in self.ranks.items()}
            flows = {'count': sum(1 for entry in self.flows.values() if entry['data']), 'tried': len(self.flows)}
            errors = dict(self.errors)
        return {'rankings': rankings, 'flows': flows, 'errors': errors}

    def attention(self, market, max_age=36*3600):
        """{symbol: {kind: rank}} from the newest ranking snapshot, or None when there is none or it is stale.

        Used by the daily focus screen, which runs before the open and accepts yesterday's session. A symbol that is
        missing from the returned dict was outside the top 100 of every list that was fetched."""
        now = self.clock()
        with self.lock:
            ranks = self.ranks.get(market)
            if not ranks or now-ranks['at'] > max_age or not ranks['kinds']:
                return None
            kinds = {key: dict(items) for key, items in ranks['kinds'].items()}
        result = {}
        for key, items in kinds.items():
            for symbol, hit in items.items():
                result.setdefault(symbol, {})[key] = hit['rank']
        return result

    def features(self, symbol, market):
        """What the AI is allowed to see for one candidate; None means "not available", not "zero"."""
        now = self.clock()
        with self.lock:
            ranks, flow = self.ranks.get(market), self.flows.get(symbol)
        rankings = None
        if ranks and now-ranks['at'] <= MAX_AGE and ranks['kinds']:
            rankings = {'as_of_minutes_ago': int((now-ranks['at'])//60)}
            for key, items in ranks['kinds'].items():
                hit = items.get(symbol)
                rankings[key+'_rank'] = hit['rank'] if hit else OUTSIDE
                # Volume/amount rankings measure change against the previous close (gainers/losers use a period).
                if hit and hit['change_pct'] is not None and key in ('volume', 'amount'):
                    rankings.setdefault('change_vs_prev_close_pct', hit['change_pct'])
        flows = None
        if flow and flow['data'] and now-flow['at'] <= FLOW_MAX_AGE:
            flows = dict(flow['data'], as_of_minutes_ago=int((now-flow['at'])//60))
        return {'rankings': rankings, 'investor_flows': flows}
