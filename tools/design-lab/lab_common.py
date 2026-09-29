"""Design-lab helpers: synthetic market context so every panel has something to show. Never used in production."""
import time


def seed_intel(engine):
    day = time.strftime('%Y-%m-%d')
    kr = {'volume': {'005930': {'rank': 1, 'change_pct': 1.42}, '000660': {'rank': 3, 'change_pct': 2.87}, '122630': {'rank': 9, 'change_pct': 0.61}},
          'amount': {'005930': {'rank': 1, 'change_pct': 1.42}, '000660': {'rank': 2, 'change_pct': 2.87}, '122630': {'rank': 12, 'change_pct': 0.61}},
          'gainers': {'000660': {'rank': 6, 'change_pct': 2.87}}, 'losers': {}}
    us = {'volume': {'TQQQ': {'rank': 2, 'change_pct': 3.1}, 'AAPL': {'rank': 5, 'change_pct': 0.44}, 'MSFT': {'rank': 11, 'change_pct': -0.21}, 'SQQQ': {'rank': 14, 'change_pct': -3.0}},
          'amount': {'TQQQ': {'rank': 3, 'change_pct': 3.1}, 'AAPL': {'rank': 4, 'change_pct': 0.44}, 'MSFT': {'rank': 8, 'change_pct': -0.21}},
          'gainers': {'TQQQ': {'rank': 4, 'change_pct': 3.1}}, 'losers': {'SQQQ': {'rank': 3, 'change_pct': -3.0}}}
    engine.intel.store_rankings('KR', kr)
    engine.intel.store_rankings('US', us)
    flows = {'005930': (182340, -45210, 951200, -120400), '000660': (-73200, 128800, -210500, 402100), '122630': (5400, -1200, 22100, 8300)}
    for symbol, (fo, inst, fo5, inst5) in flows.items():
        engine.intel.store_flow(symbol, {'date': day, 'provisional': True, 'foreigner_net_shares': fo, 'institution_net_shares': inst,
                                         'individual_net_shares': None, 'foreigner_5d_net_shares': fo5, 'institution_5d_net_shares': inst5, 'days': 5})
