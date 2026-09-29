import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from .config import SYMBOLS as BASE_SYMBOLS
from .instruments import INSTRUMENTS, SYMBOLS


class ProviderError(Exception):
    pass


class RateLimited(ProviderError):
    """Toss answered 429; calls pause until the advertised retry time."""


def route_group(path):
    """Toss meters calls per group, so a 429 in one group must not pause the others (quotes drive exits)."""
    if path in ('/api/v1/prices', '/api/v1/orderbook'):
        return 'market-data'
    if path == '/api/v1/candles':
        return 'chart'
    if path == '/api/v1/rankings':
        return 'ranking'
    if path.endswith('/investor-trading'):
        return 'trend'
    return 'calendar'


# The only Toss routes this app may call. Every one is a read-only GET; the single POST is the
# OAuth token exchange. Order, conditional-order and account routes must never match here.
READ_ONLY_ROUTES = (
    re.compile(r'^/api/v1/(prices|orderbook|candles|rankings)$'),
    re.compile(r'^/api/v1/market-calendar/(KR|US)$'),
    re.compile(r'^/api/v1/stocks/[A-Za-z0-9.\-]{1,20}/investor-trading$'),
)


def timestamp(value):
    if not value:
        return 0.0
    try:
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return d.timestamp() if d.tzinfo is not None else 0.0
    except (ValueError, TypeError):
        return 0.0


def positive(value):
    f = float(value)
    if not math.isfinite(f) or f <= 0:
        raise ProviderError('시세에 유효하지 않은 값이 있습니다.')
    return f


class DemoProvider:
    """Deliberately synthetic. Every response and ledger is namespaced demo."""
    def set_universe(self, symbols):
        if any(symbol not in SYMBOLS for symbol in symbols):
            raise ProviderError('지원하지 않는 시세 종목입니다.')
        self.quote_symbols = tuple(dict.fromkeys(symbols))

    def quotes(self):
        return {symbol: self.quote(symbol) for symbol in (getattr(self, 'quote_symbols', None) or BASE_SYMBOLS)}

    def quote(self, symbol):
        i = SYMBOLS[symbol]
        now = time.time()
        phase = sum(map(ord, symbol))
        p = round(i['demo_base'] * (1 + .012 * math.sin(now / 100 + phase)), 2)
        spread = 50 if i['currency'] == 'KRW' else .02
        return {'symbol': symbol, 'last': p, 'bid': max(.01, p-spread), 'ask': p+spread,
                'bid_size': 1000, 'ask_size': 1000, 'currency': i['currency'], 'asof': now,
                'book_asof': now, 'received': now, 'tradable': True, 'mode': 'demo',
                'session_start': now-86400, 'session_end': now+86400,
                'session': '합성 시세 · 거래 시간 제한 없음'}

    def candles(self, symbol, interval='1d'):
        if interval not in ('1d', '1m'):
            raise ProviderError('지원하지 않는 캔들 간격입니다.')
        p = SYMBOLS[symbol]['demo_base']
        step = 60 if interval == '1m' else 86400
        end = int(time.time()//step)*step
        bars = []
        for j in range(60):
            close = round(p*(1+.02*math.sin(j/6)), 2)
            bars.append({'time': end-(60-j)*step, 'open': close, 'high': round(close*1.002, 2),
                         'low': round(close*.998, 2), 'close': close, 'volume': 100000+j*1000,
                         'currency': SYMBOLS[symbol]['currency'], 'interval': interval, 'completed': True})
        return bars


class TossProvider:
    BASE = 'https://openapi.tossinvest.com'
    set_universe = DemoProvider.set_universe

    def __init__(self, config):
        self.c = config
        self.client = httpx.Client(timeout=12, follow_redirects=False)
        self.auth_lock = threading.Lock()
        self.token = ''
        self.expires = 0
        self.calendar = {}
        self.parallel = max(1, min(8, int(getattr(config, 'toss_parallel', 4) or 1)))
        self.cooldowns = {}     # group -> time until which its calls pause
        self.rates = {}         # group -> last advertised {limit, remaining, at}

    def access_token(self):
        with self.auth_lock:
            if self.token and time.time() < self.expires:
                return self.token
            if not self.c.toss_id or not self.c.toss_secret:
                raise ProviderError('서버 .env에 토스 Client ID와 Client Secret을 설정하세요.')
            try:
                r = self.client.post(self.BASE+'/oauth2/token', data={
                    'grant_type': 'client_credentials', 'client_id': self.c.toss_id,
                    'client_secret': self.c.toss_secret})
                r.raise_for_status()
                payload = r.json()
                self.token = payload['access_token']
                self.expires = time.time()+int(payload['expires_in'])-120
                return self.token
            except Exception:
                raise ProviderError('토스 인증 실패. 서버 IP 허용 목록과 API 자격증명을 확인하세요.') from None

    def get(self, path, params=None):
        # Only explicitly allow-listed read-only API routes. No brokerage order API exists in this app.
        if not any(route.match(path) for route in READ_ONLY_ROUTES):
            raise ProviderError('허용되지 않은 조회입니다.')
        group = route_group(path)
        if time.time() < self.cooldowns.get(group, 0):
            raise RateLimited(f'토스 호출 한도({group})에 걸려 잠시 조회를 쉽니다.')
        token = self.access_token()
        try:
            r = self.client.get(self.BASE+path, params=params, headers={'Authorization': 'Bearer '+token})
            self.note_rate(group, r.headers)
            if r.status_code == 401:
                with self.auth_lock:
                    self.token = ''
                raise ProviderError('토스 토큰이 만료 또는 무효화됐습니다. 다음 조회에 다시 인증합니다.')
            if r.status_code == 429:
                try:
                    wait = int(r.headers.get('Retry-After', 1))
                except (TypeError, ValueError):
                    wait = 1
                self.cooldowns[group] = time.time()+min(30, max(1, wait))
                raise RateLimited(f'토스 호출 한도({group})에 걸렸습니다. 잠시 뒤 다시 조회합니다.')
            if not 200 <= r.status_code < 300:
                raise ProviderError(f'토스 시세 조회 실패 [{self.failure_label(r)}]. 합성 시세로 대체하지 않고 거래를 차단합니다.')
            return r.json()['result']
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f'토스 시세 조회 실패 [{type(exc).__name__}]. 합성 시세로 대체하지 않고 거래를 차단합니다.') from None

    @staticmethod
    def failure_label(response):
        """HTTP status plus Toss's error code, e.g. 'HTTP 400 unsupported-ranking-duration'. Never the body text."""
        code = ''
        try:
            found = response.json().get('error', {}).get('code')
            code = found if isinstance(found, str) and len(found) <= 60 and found.replace('-', '').isalnum() else ''
        except Exception:
            pass
        return f'HTTP {response.status_code}'+(' '+code if code else '')

    def note_rate(self, group, headers):
        """Remember each group's advertised burst size, so parallel reads never exceed their own group's limit."""
        try:
            limit = int(headers.get('X-RateLimit-Limit'))
            remaining = int(headers.get('X-RateLimit-Remaining', limit))
        except (TypeError, ValueError):
            return
        if limit > 0:
            self.rates[group] = {'limit': limit, 'remaining': max(0, remaining), 'at': time.time()}

    def workers(self, tasks):
        limit = self.rates.get('market-data', {}).get('limit') or self.parallel
        return max(1, min(self.parallel, int(limit), tasks))

    def session(self, market):
        zone = ZoneInfo('Asia/Seoul' if market == 'KR' else 'America/New_York')
        date = datetime.now(zone).date().isoformat()
        key = (market, date)
        cached = self.calendar.get(key)
        if cached is None or time.time()-cached[0] > 300:
            result = self.get('/api/v1/market-calendar/'+market, {'date': date})
            today = result['today']
            if today['date'] != date:
                raise ProviderError('거래 일정의 날짜가 현재 날짜와 다릅니다.')
            self.calendar[key] = (time.time(), today)
        today = self.calendar[key][1]
        parent = (today.get('integrated') or {}) if market == 'KR' else today
        regular = parent.get('regularMarket') or {}
        start, end = timestamp(regular.get('startTime')), timestamp(regular.get('endTime'))
        auction = timestamp(regular.get('singlePriceAuctionStartTime'))
        if auction:
            end = min(end, auction)
        active = bool(start and end and start <= time.time() < end)
        return active, '정규장' if active else '정규장 외 · 모의체결 대기', start, end

    def prices(self, symbols):
        rows = self.get('/api/v1/prices', {'symbols': ','.join(symbols)})
        return {r['symbol']: r for r in rows}

    def quote(self, symbol, price=None):
        inst = SYMBOLS[symbol]
        price = price or self.prices([symbol]).get(symbol)
        if not price or price['currency'] != inst['currency']:
            raise ProviderError('시세의 종목 또는 통화가 일치하지 않습니다.')
        book = self.get('/api/v1/orderbook', {'symbol': symbol})
        if book['currency'] != inst['currency']:
            raise ProviderError('호가 통화가 일치하지 않습니다.')
        asks = [(positive(x['price']), positive(x['volume'])) for x in book.get('asks', []) if float(x['volume']) > 0]
        bids = [(positive(x['price']), positive(x['volume'])) for x in book.get('bids', []) if float(x['volume']) > 0]
        ask = min(asks) if asks else (0, 0)
        bid = max(bids) if bids else (0, 0)
        active, label, session_start, session_end = self.session(inst['market'])
        asof, book_asof = timestamp(price.get('timestamp')), timestamp(book.get('timestamp'))
        valid_book = ask[0] > 0 and bid[0] > 0 and bid[0] <= ask[0]
        return {'symbol': symbol, 'last': positive(price['lastPrice']), 'ask': ask[0], 'bid': bid[0],
                'ask_size': int(ask[1]), 'bid_size': int(bid[1]), 'currency': inst['currency'],
                'asof': asof, 'book_asof': book_asof, 'received': time.time(),
                'tradable': bool(active and valid_book and asof and book_asof),
                'session_start': session_start, 'session_end': session_end,
                'mode': 'toss', 'session': label if valid_book else '유효한 호가 없음'}

    def quotes(self):
        symbols = tuple(getattr(self, 'quote_symbols', None) or BASE_SYMBOLS)
        prices = self.prices(list(symbols))
        results = {}
        # Warm the cached session calendar once so worker threads never race to fetch it.
        for market in sorted({SYMBOLS[symbol]['market'] for symbol in symbols}):
            try:
                self.session(market)
            except ProviderError:
                continue
        workers = self.workers(len(symbols))
        if workers <= 1:
            outcomes = [(symbol, self.safe_quote(symbol, prices.get(symbol))) for symbol in symbols]
        else:
            # The order book is one call per symbol; reading them in parallel keeps exit monitoring fast.
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [(symbol, pool.submit(self.safe_quote, symbol, prices.get(symbol))) for symbol in symbols]
                outcomes = [(symbol, future.result()) for symbol, future in futures]
        for symbol, quote in outcomes:
            # No synthetic fallback and no successful timestamp for failed individual quotes.
            if quote is not None:
                results[symbol] = quote
        if not results:
            raise ProviderError('사용 가능한 토스 호가가 없습니다. 연결 설정과 거래 시간을 확인하세요.')
        return results

    def safe_quote(self, symbol, price):
        try:
            return self.quote(symbol, price)
        except ProviderError:
            return None

    def rankings(self, country, kind, duration='realtime', count=100):
        return self.get('/api/v1/rankings', {'type': kind, 'marketCountry': country,
                                             'duration': duration, 'count': count})

    def investor_trading(self, symbol, count=5):
        return self.get(f'/api/v1/stocks/{symbol}/investor-trading', {'count': count})

    def candles(self, symbol, interval='1d'):
        if interval not in ('1d', '1m'):
            raise ProviderError('지원하지 않는 캔들 간격입니다.')
        data = self.get('/api/v1/candles', {'symbol': symbol, 'interval': interval,
                                         'count': 120 if interval == '1m' else 60, 'adjusted': 'true'})
        now, rows, seen = time.time(), [], set()
        try:
            for item in data['candles']:
                asof = timestamp(item['timestamp'])
                if not asof or asof > now+5 or asof in seen:
                    continue
                currency = item.get('currency', SYMBOLS[symbol]['currency'])
                if currency != SYMBOLS[symbol]['currency']:
                    raise ValueError('candle currency mismatch')
                volume = float(item['volume'])
                if not math.isfinite(volume) or volume < 0:
                    raise ValueError('invalid volume')
                close = positive(item['closePrice'])
                row = {'time': asof, 'close': close, 'volume': volume, 'currency': currency,
                       'interval': interval, 'completed': asof+(60 if interval == '1m' else 86400) <= now}
                for name in ('open', 'high', 'low'):
                    if item.get(name+'Price') is not None:
                        row[name] = positive(item[name+'Price'])
                if interval == '1m' and not all(key in row for key in ('open', 'high', 'low')):
                    raise ValueError('minute OHLC missing')
                if all(key in row for key in ('open', 'high', 'low')):
                    if row['low'] > min(row['open'], row['close']) or row['high'] < max(row['open'], row['close']):
                        raise ValueError('invalid candle range')
                seen.add(asof)
                rows.append(row)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ProviderError('캔들 데이터의 시각·통화·가격을 검증하지 못했습니다.') from None
        return sorted(rows, key=lambda row: row['time'])
