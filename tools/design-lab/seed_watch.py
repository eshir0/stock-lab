"""A day-trading demo ledger (seed_day.py) plus conditional entries: two waiting plans and three finished ones, a trade made by a
plan, and a director report that carries a plan."""
import runpy
import time

runpy.run_path('/lab/seed_day.py')

from app import entry
from app.config import Config
from app.store import Store

cfg = Config(database_url='sqlite:////tmp/demo.db', mode='demo', password='demo-password-12345', session_secret='x'*40,
             toss_id='', toss_secret='', gemini_key='')
store = Store(cfg.database_url, cfg.mode)
now = time.time()
with store.edit() as s:
    def base(symbol):
        q = s['quotes'].get(symbol) or {}
        return q.get('ask') or {'000660': 182000.0, 'AAPL': 201.0, 'TQQQ': 76.0}.get(symbol, 100.0)

    def make(symbol, name, market, currency, kind, level, dead, minutes, **extra):
        fields = {'type': kind, 'level': round(level, 2), 'invalidate': round(dead, 2), 'expires': now+minutes*60,
                  'plan': {'target_weight_pct': 30, 'stop_loss_pct': 2, 'take_profit_pct': 4, 'max_holding_minutes': 90}}
        watch = entry.make_watch(fields, symbol=symbol, name=name, market=market, currency=currency, horizon='intraday',
                                 reference=base(symbol), summary=extra.pop('summary', '5분봉 저항선 바로 아래에서 거래량이 늘고 있어 돌파하면 매수하고, 저항에서 밀리면 지지선 근처 눌림을 노립니다.'),
                                 engine='Claude · claude-opus-5-5', run_id='seed-w', generation=1, now=now-extra.pop('age', 600))
        watch.update(extra)
        return watch
    a = base('000660')
    b = base('AAPL')
    t = base('TQQQ')
    s['watches'] = [
        make('TQQQ', 'ProShares UltraPro QQQ', 'US', 'USD', 'breakout', t*.9, t*.88, 35, age=4000, status='filled', closed=now-2400, outcome='2.6053주 자동 모의매수', summary='돌파 후 거래량 확인.'),
        make('NVDA', 'NVIDIA', 'US', 'USD', 'pullback', 120.0, 117.0, 45, age=3500, status='expired', closed=now-1500, outcome='유효 시간 안에 조건이 충족되지 않았습니다.'),
        make('AMD', 'AMD', 'US', 'USD', 'breakout', 150.0, 146.0, 30, age=2500, status='invalid', closed=now-900, outcome='가격이 무효 기준 아래로 내려가 폐기했습니다.'),
        make('000660', 'SK하이닉스', 'KR', 'KRW', 'breakout', a*1.006, a*.992, 50, hits=2, last_price=a, note='돌파했지만 거래량이 받쳐주지 않아 기다립니다.'),
        make('AAPL', 'Apple', 'US', 'USD', 'pullback', b*.991, b*.976, 75, last_price=b),
    ]
    for trade in reversed(s['trades']):
        if trade['side'] == 'BUY':
            trade['entry_watch'] = s['watches'][0]['id']
            break
    run = s['runs'][-1]
    for report in run['reports']:
        if report.get('role') == 'director':
            report.update(stance='HOLD', entry_type='breakout', entry_level=round(a*1.006, 2), entry_invalidate=round(a*.992, 2), entry_minutes=50)
    run['watch'] = {'status': 'waiting', 'id': s['watches'][3]['id'], 'note': entry.describe(s['watches'][3])}
    # an analysis that reused earlier research: the three research reports are marked, the run says how many calls it saved
    run['reuse'] = {'from_run': 'seed-prev', 'age_minutes': 23, 'saved_calls': 3}
    for report in run['reports']:
        if report.get('role') in ('planner', 'fundamental', 'news'):
            report.update(reused=True, age_minutes=23)
store.release()
print('seeded watches')
