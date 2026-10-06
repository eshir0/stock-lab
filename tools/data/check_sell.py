import random, sys
import pandas as pd
sys.path.insert(0, '/app')
from app import rules
from research_sell import sell_signals, DATA
files = sorted((DATA/'daily'/'US').glob('*.parquet'))
random.seed(2); bad = n = 0
for path in random.sample(files, 10):
    df = pd.read_parquet(path).dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
    if len(df) < 100: continue
    vec = sell_signals(df); candles = df[['open', 'high', 'low', 'close', 'volume']].to_dict('records')
    for t in random.sample(range(25, len(df)), 120):
        ref = any(v == 'SELL' for v in rules.signals(candles[:t+1]).values()); n += 1
        bad += ref != bool(vec[t])
print('checked', n, 'mismatches', bad)
