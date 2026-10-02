"""Check that backtest.signals matches the site's app/rules.signals bar for bar on real archive files."""
import random, sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, '/app')
from app import rules
from backtest import signals, DATA
files = sorted((DATA/'daily'/'KR').glob('*.parquet'))
random.seed(1)
checked = mismatches = 0
for path in random.sample(files, min(12, len(files))):
    df = pd.read_parquet(path).dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
    if len(df) < 100:
        continue
    vec = signals(df)
    candles = df[['open', 'high', 'low', 'close', 'volume']].to_dict('records')
    for t in random.sample(range(25, len(df)), 150):
        ref = rules.signals(candles[:t+1])
        for k in rules.RULES:
            checked += 1
            if (ref[k] == 'BUY') != bool(vec[k].iloc[t]):
                mismatches += 1
                if mismatches < 6:
                    print('MISMATCH', path.stem, t, k, ref[k], vec[k].iloc[t])
print(f'checked {checked} rule values, mismatches {mismatches}')
