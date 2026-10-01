"""Design lab: the month seed plus what the verification panel shows (three weeks into the plan, rule-triggered decisions)."""
import runpy

runpy.run_path('/lab/seed.py')

from app.config import Config
from app.engine import Engine
from app.providers import DemoProvider
from app.store import Store

cfg = Config(database_url='sqlite:////tmp/demo.db', mode='demo', password='demo-password-12345', session_secret='x'*40,
             toss_id='', toss_secret='', gemini_key='')
store = Store(cfg.database_url, cfg.mode)
engine = Engine(cfg, store, DemoProvider())
with store.edit() as s:
    for i, e in enumerate(s.get('evaluations', [])):
        if e.get('horizon') == 'month':
            e['trigger_side'] = 'SELL' if i % 4 == 0 else 'BUY'
    s['verification']['started_at'] -= 20*86400
    s['started_at'] -= 20*86400
engine.refresh_benchmark()
store.release()
print('seeded verification')
