"""Live-trading readiness layer (STRUCTURE ONLY - locked in this build).

Nothing in this package can send an order. It defines the pieces a safe live mode needs: the master
lock, absolute limits, an order gate (preview -> confirm -> execute), an order lifecycle,
ledger-vs-broker reconciliation, broker-side protective-order planning and a shadow recorder. Only the
shadow recorder runs today, and it never talks to a broker. See docs/LIVE_TRADING.md.

Keep this module free of imports: config.py imports the lock, so heavy imports here would create cycles.
"""
