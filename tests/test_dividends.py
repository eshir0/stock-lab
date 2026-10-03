"""Realism: dividends are paid into the paper account (after withholding) for positions bought before the ex-date, they
count in the round trip's result, and this year's US gains get a Korean tax estimate."""
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import scorecard
from app.evidence import EvidenceStore
from test_month_desk import DAY, desk, open_position  # noqa: F401  (fixture)

SEOUL = ZoneInfo('Asia/Seoul')


def today(days=0):
    return (datetime.now(SEOUL)+timedelta(days=days)).date().isoformat()


def give_pack(engine, folder, dividends, symbol='005930'):
    (folder/f'{symbol}.json').write_text(json.dumps({'symbol': symbol, 'built_at': datetime.now().isoformat(timespec='seconds'), 'as_of_bar': today(),
                                                     'dividends': dividends}), encoding='utf-8')
    engine.evidence = EvidenceStore(folder)


def age(engine, days):
    """Pretend the experiment started and the position was bought `days` ago."""
    with engine.store.edit() as s:
        s['started_at'] -= days*DAY
        for t in s['trades']:
            t['time'] -= days*DAY


def pay(engine):
    engine.dividends_at = 0
    engine.credit_dividends()
    return engine.store.read()


def test_a_position_bought_before_the_ex_date_is_paid_once_after_withholding(desk, tmp_path):
    open_position(desk, quantity=2)
    age(desk, 3)
    give_pack(desk, tmp_path, [{'ex_date': today(-1), 'amount': 1000.0}])
    cash = desk.store.read()['cash']['KRW']
    state = pay(desk)
    assert state['cash']['KRW'] == pytest.approx(cash+2000*(1-.154))
    d = state['dividends'][0]
    assert (d['gross'], d['tax'], d['net'], d['ex_date']) == (2000.0, 308.0, 1692.0, today(-1))
    assert any('배당 입금' in e['message'] for e in state['events'])
    assert pay(desk)['cash']['KRW'] == state['cash']['KRW']                                   # never twice


@pytest.mark.parametrize('ex_days, bought_days', [(0, 0), (1, 3), (-5, 3)])
def test_no_payment_for_a_buy_on_or_after_the_ex_date_a_future_ex_date_or_before_the_experiment(desk, tmp_path, ex_days, bought_days):
    open_position(desk)
    age(desk, bought_days)
    give_pack(desk, tmp_path, [{'ex_date': today(ex_days), 'amount': 1000.0}])
    assert not pay(desk).get('dividends')


def test_no_pack_means_no_payment(desk, tmp_path):
    open_position(desk)
    age(desk, 3)
    desk.evidence = EvidenceStore(tmp_path)
    assert not pay(desk).get('dividends')


def test_us_dividends_withhold_15_percent():
    from app.engine import Engine
    assert float(Engine.WITHHOLDING['USD']) == .15 and float(Engine.WITHHOLDING['KRW']) == .154


def test_the_dividend_belongs_to_the_round_trip_that_held_the_shares():
    trades = [{'symbol': '005930', 'side': 'BUY', 'quantity': 1, 'price': 100, 'fee': 0, 'time': 1},
              {'symbol': '005930', 'side': 'SELL', 'quantity': 1, 'price': 100, 'realized': 0, 'time': 3}]
    plain = scorecard.report(trades)['expectancy_pct']
    paid = scorecard.report(trades, [{'symbol': '005930', 'net': 5, 'time': 2}])
    assert plain == 0 and paid['expectancy_pct'] == pytest.approx(5.0)
    late = scorecard.report(trades, [{'symbol': '005930', 'net': 5, 'time': 4}])
    assert late['expectancy_pct'] == 0                                                        # after the sale: not this trip


def test_the_us_tax_estimate():
    now = time.time()
    trades = [{'side': 'SELL', 'currency': 'USD', 'realized': 3000, 'time': now},
              {'side': 'SELL', 'currency': 'USD', 'realized': -500, 'time': now},
              {'side': 'SELL', 'currency': 'KRW', 'realized': 9e9, 'time': now},
              {'side': 'SELL', 'currency': 'USD', 'realized': 9e9, 'time': now-400*DAY}]
    t = scorecard.us_capital_gains_tax(trades, 1400.0, now)
    assert t['realized_usd'] == 2500 and t['realized_krw'] == 3_500_000 and t['tax_krw'] == 220_000
    assert scorecard.us_capital_gains_tax(trades, None, now)['tax_krw'] is None
    assert scorecard.us_capital_gains_tax([], 1400.0, now)['tax_krw'] == 0
