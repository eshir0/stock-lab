"""TossProvider read-only guarantees, rate-limit handling and parallel order-book reads (no network)."""
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import Config
from app.providers import ProviderError, RateLimited, TossProvider, route_group


class FakeResponse:
    def __init__(self, status=200, result=None, headers=None):
        self.status_code, self.result, self.headers = status, result, headers or {}

    def json(self):
        return {'result': self.result}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError('http error')


class FakeClient:
    def __init__(self, *responses):
        self.calls, self.responses = [], list(responses)

    def get(self, url, params=None, headers=None):
        self.calls.append(('GET', url, params))
        return self.responses.pop(0) if self.responses else FakeResponse(result=[])

    def post(self, url, **kwargs):
        raise AssertionError('no POST is expected once a token is cached')


def provider(*responses):
    config = Config(mode='toss', toss_id='id', toss_secret='secret', toss_parallel=4)
    toss = TossProvider(config)
    toss.client = FakeClient(*responses)
    toss.token, toss.expires = 'cached-token', time.time()+3600
    return toss


@pytest.mark.parametrize('path', ['/api/v1/orders', '/api/v1/orders/abc/cancel', '/api/v1/conditional-orders',
                                  '/api/v1/accounts', '/api/v1/holdings', '/api/v1/buying-power', '/oauth2/token',
                                  '/api/v1/stocks/005930/warnings', '/api/v1/stocks/../orders/investor-trading'])
def test_trading_account_and_unlisted_routes_never_reach_the_network(path):
    toss = provider()
    with pytest.raises(ProviderError, match='허용되지 않은'):
        toss.get(path)
    assert toss.client.calls == []


def test_allowed_routes_are_plain_gets():
    toss = provider(FakeResponse(result={'rankings': []}), FakeResponse(result={'records': []}))
    toss.rankings('KR', 'MARKET_TRADING_VOLUME', 'realtime', 100)
    toss.investor_trading('005930', 5)
    assert toss.client.calls == [
        ('GET', 'https://openapi.tossinvest.com/api/v1/rankings',
         {'type': 'MARKET_TRADING_VOLUME', 'marketCountry': 'KR', 'duration': 'realtime', 'count': 100}),
        ('GET', 'https://openapi.tossinvest.com/api/v1/stocks/005930/investor-trading', {'count': 5})]


def test_a_429_pauses_only_its_own_group_until_the_retry_time():
    toss = provider(FakeResponse(429, headers={'Retry-After': '3'}), FakeResponse(result={'rankings': []}))
    with pytest.raises(RateLimited):
        toss.get('/api/v1/prices', {'symbols': 'AAPL'})
    assert 2 <= toss.cooldowns['market-data']-time.time() <= 3.5
    with pytest.raises(RateLimited):
        toss.get('/api/v1/orderbook', {'symbol': 'AAPL'})              # same group: paused, no network call
    assert len(toss.client.calls) == 1
    assert toss.get('/api/v1/rankings', {}) == {'rankings': []}       # another group keeps working
    toss.cooldowns['market-data'] = 0
    toss.client.responses.append(FakeResponse(result=[]))
    assert toss.get('/api/v1/prices', {'symbols': 'AAPL'}) == []


def test_the_retry_pause_is_capped_and_tolerates_garbage_headers():
    toss = provider(FakeResponse(429, headers={'Retry-After': '9999'}), FakeResponse(429, headers={'Retry-After': 'soon'}))
    for _ in range(2):
        toss.cooldowns.clear()
        with pytest.raises(RateLimited):
            toss.get('/api/v1/prices', {'symbols': 'AAPL'})
        assert toss.cooldowns['market-data']-time.time() <= 30.5


def test_each_group_remembers_its_own_advertised_limit():
    toss = provider(FakeResponse(result=[], headers={'X-RateLimit-Limit': '15', 'X-RateLimit-Remaining': '12'}),
                    FakeResponse(result={'rankings': []}, headers={'X-RateLimit-Limit': '2', 'X-RateLimit-Remaining': '1'}))
    toss.get('/api/v1/orderbook', {'symbol': 'AAPL'})
    toss.get('/api/v1/rankings', {})
    assert toss.rates['market-data']['limit'] == 15 and toss.rates['ranking']['limit'] == 2
    assert toss.workers(7) == 4                                         # the small ranking limit does not slow quotes
    toss.rates['market-data']['limit'] = 2
    assert toss.workers(7) == 2 and toss.workers(1) == 1
    toss.rates.clear()
    assert toss.workers(7) == 4 and toss.workers(2) == 2


def test_route_groups():
    assert route_group('/api/v1/prices') == route_group('/api/v1/orderbook') == 'market-data'
    assert route_group('/api/v1/candles') == 'chart' and route_group('/api/v1/rankings') == 'ranking'
    assert route_group('/api/v1/stocks/005930/investor-trading') == 'trend'
    assert route_group('/api/v1/market-calendar/KR') == 'calendar'


def stamp(zone):
    return datetime.now(ZoneInfo(zone)).isoformat()


def calendar(zone, market):
    now = datetime.now(ZoneInfo(zone))
    regular = {'startTime': (now-timedelta(hours=1)).isoformat(), 'endTime': (now+timedelta(hours=1)).isoformat()}
    today = {'date': now.date().isoformat(), **({'integrated': {'regularMarket': regular}} if market == 'KR' else {'regularMarket': regular})}
    return {'today': today}


def test_order_books_are_read_in_parallel_and_one_failure_does_not_lose_the_rest():
    toss = provider()
    toss.set_universe(['005930', '000660', 'AAPL', 'MSFT'])
    gauge = {'now': 0, 'peak': 0}
    lock = threading.Lock()

    def fake_get(path, params=None):
        if path == '/api/v1/prices':
            currency = {'005930': 'KRW', '000660': 'KRW', 'AAPL': 'USD', 'MSFT': 'USD'}
            return [{'symbol': s, 'timestamp': stamp('Asia/Seoul'), 'lastPrice': '100', 'currency': currency[s]}
                    for s in params['symbols'].split(',')]
        if path == '/api/v1/market-calendar/KR':
            return calendar('Asia/Seoul', 'KR')
        if path == '/api/v1/market-calendar/US':
            return calendar('America/New_York', 'US')
        assert path == '/api/v1/orderbook'
        with lock:
            gauge['now'] += 1
            gauge['peak'] = max(gauge['peak'], gauge['now'])
        time.sleep(0.05)
        with lock:
            gauge['now'] -= 1
        if params['symbol'] == '000660':
            raise ProviderError('one book is down')
        currency = 'KRW' if params['symbol'] in ('005930', '000660') else 'USD'
        return {'currency': currency, 'timestamp': stamp('Asia/Seoul'),
                'asks': [{'price': '101', 'volume': '10'}], 'bids': [{'price': '99', 'volume': '12'}]}
    toss.get = fake_get
    quotes = toss.quotes()
    assert sorted(quotes) == ['005930', 'AAPL', 'MSFT']
    assert 1 < gauge['peak'] <= 4
    assert quotes['005930']['tradable'] and quotes['AAPL']['bid'] == 99 and quotes['AAPL']['ask'] == 101


def test_all_books_failing_is_still_an_error_never_a_synthetic_quote():
    toss = provider()
    toss.set_universe(['AAPL'])

    def fake_get(path, params=None):
        if path == '/api/v1/prices':
            return [{'symbol': 'AAPL', 'timestamp': stamp('Asia/Seoul'), 'lastPrice': '100', 'currency': 'USD'}]
        if path.startswith('/api/v1/market-calendar'):
            return calendar('America/New_York', 'US')
        raise ProviderError('down')
    toss.get = fake_get
    with pytest.raises(ProviderError, match='사용 가능한 토스 호가가 없습니다'):
        toss.quotes()


def test_failures_name_the_http_status_and_toss_error_code_but_never_the_body():
    class Failing(FakeResponse):
        def json(self):
            return {'error': {'code': 'unsupported-ranking-duration', 'message': 'secret-looking details'}}
    class NoResult(FakeResponse):
        def json(self):
            return {}
    toss = provider(Failing(400), FakeResponse(500), NoResult(200))
    with pytest.raises(ProviderError) as coded:
        toss.get('/api/v1/rankings', {})
    assert 'HTTP 400 unsupported-ranking-duration' in str(coded.value) and 'secret-looking' not in str(coded.value)
    with pytest.raises(ProviderError) as bare:
        toss.get('/api/v1/rankings', {})
    assert '[HTTP 500]' in str(bare.value)
    with pytest.raises(ProviderError) as broken:                # a success body without a result is still a clear error
        toss.get('/api/v1/rankings', {})
    assert '조회 실패 [KeyError]' in str(broken.value)


def test_a_redirect_is_a_failure_like_any_other_non_success_status():
    toss = provider(FakeResponse(302, result=[{'symbol': 'AAPL'}]))
    with pytest.raises(ProviderError, match=r'\[HTTP 302\]'):
        toss.get('/api/v1/prices', {'symbols': 'AAPL'})
