"""Backtest harness for evaluating strategies against historical data.

Two execution modes:
1. SIGNAL REPLAY (this commit MVP) — load existing past signals from
   the `signals` table, fetch historical price data, simulate exits
   with configurable stop/TP/timeout. Lets Roy try "what if the stop
   were tighter on funding_fade" without N=30+ paper validation.
2. FULL TAPE REPLAY (future) — replay historical asset_contexts/wallet
   buys through detector state, re-emit signal candidates fresh, then
   simulate. Requires historical tape persistence (not yet built).

Engineer's R1 of probability poll identified this as the single highest-
leverage missing infrastructure. Shipping the MVP unblocks rapid
strategy iteration.
"""
