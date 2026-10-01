"""Build a realistic demo ledger through the app's own engine (demo mode, isolated SQLite in /tmp)."""
import random
import time

from app.config import Config
from app.engine import Engine
from app.evaluation import record_decision
from app.providers import DemoProvider
from app.rules import RULES
from app.store import Store
from lab_common import seed_intel

random.seed(11)
cfg = Config(database_url='sqlite:////tmp/demo.db', mode='demo', password='demo-password-12345', session_secret='x'*40,
             toss_id='', toss_secret='', gemini_key='')
store = Store(cfg.database_url, cfg.mode)
engine = Engine(cfg, store, DemoProvider())
engine.boot()
engine.new_experiment(1000000, 1000, '단타 실험 1차', 30, 'intraday',
                      {'include_leveraged_etfs': True, 'risk_per_trade_pct': .5, 'daily_loss_limit_pct': 2, 'horizon': 'intraday', 'max_holding_minutes': 120, 'max_position_pct': 100})
engine.refresh()
seed_intel(engine)
engine.set_execution('auto')
engine.start()
for symbol in ('005930', 'AAPL', '000660', 'TQQQ', '005930', None, None):
    if symbol:
        engine.request_cycle(symbol)
    else:
        with store.edit() as s:
            s['next_run'] = time.time()
    engine.cycle()
    time.sleep(.3)

now = time.time()
symbols = ['AAPL', 'MSFT', 'TQQQ', 'SQQQ', '005930', '000660']
with store.edit() as s:
    # Equity history: three hours of gently rising, noisy curves.
    history, krw, usd = [], 1000000.0, 1000.0
    for i in range(180):
        t = now-(179-i)*60
        krw *= 1+random.gauss(.00006, .0009)
        usd *= 1+random.gauss(.00004, .0011)
        history.append({'time': t, 'KRW': round(krw, 2), 'USD': round(usd, 2), 'fresh': {'KRW': True, 'USD': True}})
    s['history'] = history
    s['daily_ai'] = {time.strftime('%Y-%m-%d', time.gmtime(now)): 14}
    s['events'].insert(0, {'time': now-5400, 'message': 'AI 종목 선정 실패로 순서대로 선택합니다: [Claude 사용량 소진] 대기 중', 'level': 'warning'})
    # Scored decisions: a plausible mix of stances, engines, outcomes and rule signals.
    picks = ['BUY']*8+['HOLD']*13+['SELL']*4+['BUY']*3
    engines = ['Claude · claude-opus-5-5']*16+['Codex · gpt-6.1-sol']*6+['gemini-3.5-flash']*4
    for i in range(26):
        symbol, stance = random.choice(symbols), picks[i]
        market = 'KR' if symbol[0].isdigit() else 'US'
        t = now-(26-i)*11*60-3600
        price = 100.0
        quote = {'bid': price, 'ask': price, 'asof': t, 'received': t, 'session_end': now+3600}
        pool = [x for x in symbols if (x[0].isdigit()) == (market == 'KR') and x != symbol]
        others = {x: price for x in random.sample(pool, k=min(2, len(pool)))}
        rules = {name: random.choice(['BUY', 'SELL', 'HOLD', 'HOLD', None]) for name in RULES}
        entry = record_decision(s, run_id=f'seed-{i}', symbol=symbol, market=market,
                                decision={'stance': stance, 'engine': engines[i], 'target_weight_pct': 10}, quote=quote,
                                candidates=others, selected_by=random.choice(['ai', 'ai', 'ai', 'user', 'server']), cost_bps=38+random.random()*10,
                                now=t, rules=rules)
        entry['action'] = {'BUY': random.choice(['filled', 'filled', 'blocked']), 'SELL': 'filled', 'HOLD': 'hold'}[stance]
        if i < 23:
            drift = random.gauss(.03, .18)
            for horizon in (30, 60):
                returns = {x: round(random.gauss(drift*horizon/30, .55), 4) for x in [symbol, *others]}
                entry['outcomes'][str(horizon)] = {'time': t+horizon*60, 'at_close': False, 'returns': returns}
    # A few shadow orders that would have been blocked, for the readiness panel.
    for i, (sym, cur, side, qty, price, blocked) in enumerate([
            ('005930', 'KRW', 'BUY', 4, 70100, ['max-order', 'max-daily-notional']), ('TQQQ', 'USD', 'BUY', 1, 76.8, ['symbol-not-allowed']),
            ('AAPL', 'USD', 'BUY', 1, 201.3, []), ('AAPL', 'USD', 'SELL', 1, 201.9, []), ('000660', 'KRW', 'BUY', 1, 182300, [])]):
        s.setdefault('shadow_orders', []).append({
            'time': now-(6-i)*380, 'symbol': sym, 'market': 'KR' if cur == 'KRW' else 'US', 'currency': cur, 'side': side, 'quantity': qty,
            'order_type': 'LIMIT', 'limit_price': str(price), 'notional': str(round(price*qty, 2)), 'source': 'exit' if side == 'SELL' else 'ai',
            'proposal_id': f'seed-{i}', 'would_submit': not blocked, 'blocked_by': blocked, 'suggested_quantity': 1 if blocked and blocked[0] == 'max-order' else 0,
            'protective': {'stop_trigger': str(round(price*.985, 2)), 'take_profit_trigger': str(round(price*1.03, 2))} if side == 'BUY' and not blocked else None,
            'sim_status': 'filled'})
# Today's focus list, built by the real engine from synthetic uptrend candles (the demo provider's own daily candles are a flat sine wave).
from app.instruments import CATALOGUE
import math
_daily = {c['symbol']: .0012+(i % 7)*.0007 for i, c in enumerate(CATALOGUE)}
_spread = {c['symbol']: .012 for c in CATALOGUE}                       # a ~2.4% range: liquid but not lively
for name in ('NVDA', 'TSLA', 'AMD', '000660', '012450', 'TQQQ', 'SQQQ', '122630', '034020', '329180'):
    _spread[name] = .03                                              # lively names (~6% range)
for name in ('AAPL', 'MSFT', 'SPY', 'QQQ', '069500', '005930'):
    _spread[name] = .004                                             # calm: below the day-trading floor
_spread['PLTR'] = .09                                                # far too wild
_daily['META'] = -.035                                               # a five-day freefall
_spread['META'] = .03
_daily['AMZN'] = -.004                                               # a downtrend that still moves: allowed for a day trader
_spread['AMZN'] = .03

def _candles(symbol, interval='1d', count=None, _orig=engine.provider.candles):
    if interval != '1d':
        return _orig(symbol, interval)
    end = int(time.time()//86400)*86400
    base = 100.0
    return [{'time': end-(40-i)*86400, 'open': base*(1+_daily[symbol])**i, 'high': base*(1+_daily[symbol])**i*(1+_spread[symbol]),
             'low': base*(1+_daily[symbol])**i*(1-_spread[symbol]), 'close': base*(1+_daily[symbol])**i, 'volume': 1e9, 'currency': 'USD',
             'interval': '1d', 'completed': True} for i in range(40)]
engine.provider.candles = _candles
engine.focus_pause = 0
engine.start()
engine.refresh()
engine.refresh_focus()
with store.edit() as s:
    # a held name that left the list, so the rotation block is populated
    s['positions'].setdefault('005930', {'quantity': 3, 'average': 70000.0, 'cost_basis': 210000.0, 'strategy_mode': 'intraday',
                                          'stop_price': 68000.0, 'take_profit_price': 74000.0, 'expires_at': time.time()+7200})
    focus = s['focus'].get('KR')
    if focus and '005930' not in [p['symbol'] for p in focus['picks']]:
        s['positions']['005930']['rotation'] = {'action': 'keep', 'pnl_pct': 1.2, 'trend_ok': True, 'at': time.time(), 'session_date': focus['session_date'],
                                                 'reason': '추세가 유지돼 강제로 팔지 않고 손절·익절·보유시간 규칙에 맡깁니다. 추가 매수는 하지 않습니다.'}
with store.edit() as s:
    for record in s['focus_history']:
        record['result'] = {'pick_pct': .4, 'pool_pct': .1, 'fixed_pct': None, 'excess_pool_pct': .3, 'excess_fixed_pct': None,
                            'n_picks': 3, 'n_pool': 12, 'range_pick_pct': 5.8, 'range_pool_pct': 3.1, 'range_excess_pct': 2.7}
with store.edit() as s:
    # a fractional US position, so the positions table and the sizing text can show one
    s['positions']['AAPL'] = {'quantity': 0.2727, 'average': 733.4, 'cost_basis': 200.0, 'strategy_mode': 'intraday', 'horizon': 'intraday',
                              'stop_price': 725.0, 'take_profit_price': 745.0, 'expires_at': time.time()+3600}
engine.stop()
store.release()
print('seeded')
