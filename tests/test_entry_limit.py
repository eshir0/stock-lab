"""Waiting conditional entries (2026-10-07): six per market instead of three in all, and when a market is full a new plan
takes the place of the weakest one only if it ranks higher (evidence first, then how close its level is to the price)."""
import time

from app import entry
from test_month_desk import desk  # noqa: F401  (fixture)


def plan(symbol, level, sign, created, price=100.0, market='US'):
    return {'id': symbol, 'symbol': symbol, 'name': symbol, 'market': market, 'status': 'waiting', 'level': level,
            'evidence_sign': sign, 'created': created, 'reference': price, 'last_price': price}


def test_priority_puts_evidence_first_then_distance_then_freshness():
    assert entry.priority(plan('A', 99, 'for', 1)) > entry.priority(plan('B', 99.5, 'mixed', 2))        # evidence first
    assert entry.priority(plan('A', 99, 'mixed', 1)) > entry.priority(plan('B', 95, 'mixed', 2))        # then nearer level
    assert entry.priority(plan('A', 99, 'none', 2)) > entry.priority(plan('B', 99, 'mixed', 1))         # then the newer
    state = {'watches': [plan('A', 97, 'against', 1), plan('B', 99, 'for', 2), plan('C', 90, 'for', 3, market='KR')]}
    assert entry.weakest(state, 'US')['id'] == 'A' and entry.weakest(state, 'KR')['id'] == 'C'


def _fill_us(desk, sign):
    now = time.time()
    with desk.store.edit() as s:
        s['watches'] = [dict(plan(f'X{i}', 95.0, sign, now-100), symbol='MSFT', expires=now+3600) for i in range(entry.MAX_WAITING)]


def _decision():
    return {'stance': 'HOLD', 'entry_type': 'pullback', 'entry_level': 0, 'entry_invalidate': 0, 'entry_minutes': 600,
            'target_weight_pct': 10, 'stop_loss_pct': 4, 'take_profit_pct': 12, 'max_holding_minutes': 20160, 'summary': 's'}


def _try(desk, sign):
    desk.provider.prices['AAPL'] = 200.0
    quote = desk.provider.quote('AAPL')
    decision = dict(_decision(), entry_level=198.0, entry_invalidate=190.0)
    with desk.store.edit() as s:
        run = {'id': 'r', 'evidence': {'sign': sign}}
        desk.plan_entry(s, 'AAPL', decision, quote, run, s['generation'], time.time())
    return desk.store.read(), run


def test_a_stronger_plan_replaces_the_weakest_when_the_market_is_full(desk):
    _fill_us(desk, 'against')
    state, run = _try(desk, 'for')
    waiting = entry.waiting(state)
    assert run['watch']['status'] == 'waiting' and len([w for w in waiting if w['market'] == 'US']) == entry.MAX_WAITING
    assert any(w['symbol'] == 'AAPL' and w['evidence_sign'] == 'for' for w in waiting)
    assert sum(w['status'] == 'replaced' for w in state['watches']) == 1


def test_a_weaker_plan_is_declined_with_the_reason(desk):
    _fill_us(desk, 'for')
    state, run = _try(desk, 'against')
    assert run['watch']['status'] == 'rejected' and '시장별 6개' in run['watch']['note']
    assert not any(w['symbol'] == 'AAPL' for w in entry.waiting(state))


def test_the_limit_counts_each_market_on_its_own(desk):
    now = time.time()
    with desk.store.edit() as s:
        s['watches'] = [dict(plan(f'K{i}', 95.0, 'for', now-100, market='KR'), symbol='005930', expires=now+3600)
                        for i in range(entry.MAX_WAITING)]
    state, run = _try(desk, 'against')
    assert run['watch']['status'] == 'waiting'                          # the Korean plans do not fill the US places
