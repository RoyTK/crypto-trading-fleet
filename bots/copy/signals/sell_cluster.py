"""Sell-cluster detector — the long-side stop signal.

Per brainstorm 2026-05-30 (Trader's R1): "sell-cluster as LONG-SIDE STOPS
first." This is not a separate strategy — it's a smarter exit rule that
replaces the static stop/TP/timeout triad for any open shadow/live position
when the cohort itself is rotating out of the token.

Structurally mirrors ClusterDetector but with three asymmetries:

1. **Lower wallet threshold** (2 vs 3 for buys). Exits should fire BEFORE
   the slide accelerates. Cost of one false-positive sell-cluster is just
   "we exited a paper-hands wallet too early"; cost of a missed sell-cluster
   is "we ride a -50% leg down with the cohort." Asymmetric — favor speed.

2. **Direction is `exit`** (vs buy's `long`). Downstream consumers
   (write_cluster_detection dedup, shadow_signals, executor) route on this
   field. signal_type='sell_cluster' is also distinct from 'cluster_buy'
   so the dedup primitive keys them independently.

3. **No sizing / position-opening logic.** Sell-cluster fires → caller
   checks for open positions in the same token → close any that exist.
   No new position is ever opened on a sell signal.

State: same in-memory deque pattern as ClusterDetector. Independent state
from the buy detector — a token can simultaneously have an active buy
window AND a sell window (e.g. paper-hands wallets exiting while strong
hands keep buying).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from time import time
from typing import Optional

from bots.copy.config import (
    SELL_CLUSTER_MIN_NOTIONAL_PER_WALLET_USD,
    SELL_CLUSTER_MIN_WALLETS,
    SELL_CLUSTER_WINDOW_MINUTES,
)
from bots.copy.signals.base import SignalCandidate
from bots.copy.venue.helius_solana import WalletSellEvent


WINDOW_SECONDS = SELL_CLUSTER_WINDOW_MINUTES * 60


@dataclass
class _TokenSellWindow:
    sells: deque = field(default_factory=deque)


class SellClusterDetector:
    """Stateful sell-cluster detector. Single instance per bot loop."""

    def __init__(self) -> None:
        self._windows: dict[tuple[str, str], _TokenSellWindow] = {}
        self._already_fired: dict[tuple[str, str], int] = {}

    def observe_sell(self, ev: WalletSellEvent) -> None:
        """Push a wallet sell event into the rolling window."""
        if ev.notional_usd < SELL_CLUSTER_MIN_NOTIONAL_PER_WALLET_USD:
            return  # too small — sub-$1k sells are wallet rebalancing noise
        key = (ev.chain, ev.token_mint)
        w = self._windows.setdefault(key, _TokenSellWindow())
        w.sells.append((ev.wallet_address, ev.timestamp_ms, ev.notional_usd))

    def _prune(self, key: tuple[str, str], now_ms: int) -> None:
        cutoff = now_ms - WINDOW_SECONDS * 1000
        w = self._windows.get(key)
        if w is None:
            return
        while w.sells and w.sells[0][1] < cutoff:
            w.sells.popleft()

    def evaluate(self, now_ms: Optional[int] = None) -> list[SignalCandidate]:
        """Return sell-cluster candidates for any (chain, token) that has
        ≥SELL_CLUSTER_MIN_WALLETS distinct wallets selling above the
        per-wallet floor within the rolling window.

        Unlike the buy detector, no token-meta validation is needed for
        exits — if smart money is rotating out, the token's age and volume
        history don't change the urgency.
        """
        ts = now_ms or int(time() * 1000)
        candidates: list[SignalCandidate] = []
        for key in list(self._windows.keys()):
            self._prune(key, ts)
            w = self._windows[key]
            if not w.sells:
                continue

            chain, token = key
            distinct_wallets: dict[str, float] = {}
            for wallet, _ts_ms, notional in w.sells:
                distinct_wallets[wallet] = max(distinct_wallets.get(wallet, 0.0), notional)
            qualifying_notionals: dict[str, float] = {
                w_addr: n for w_addr, n in distinct_wallets.items()
                if n >= SELL_CLUSTER_MIN_NOTIONAL_PER_WALLET_USD
            }
            qualifying = list(qualifying_notionals.keys())
            cluster_size = len(qualifying)
            if cluster_size < SELL_CLUSTER_MIN_WALLETS:
                continue

            # Re-fire suppression. The downstream cluster_detections table
            # has its own atomic dedup with a configurable window
            # (24h default), but the in-memory suppression here prevents
            # the bot from firing dozens of sell-cluster signals into the
            # same 15-min window for a chain-of-sells event. The DB dedup
            # then takes care of cross-window re-firing.
            last = self._already_fired.get(key, 0)
            if (ts - last) < WINDOW_SECONDS * 1000:
                continue

            total_notional = sum(qualifying_notionals.values())
            avg_notional = total_notional / cluster_size

            candidates.append(SignalCandidate(
                signal_type="sell_cluster",
                asset=token,
                chain=chain,
                direction="exit",
                cluster_size=cluster_size,
                # Sell-cluster doesn't carry stop/TP/timeout — it IS the
                # exit. Caller closes whatever position exists at market.
                stop_pct=None,
                take_profit_pct=None,
                timeout_hours=None,
                payload={
                    "wallets": qualifying,
                    "wallet_notionals": qualifying_notionals,
                    "cluster_size": cluster_size,
                    "total_notional_usd": total_notional,
                    "avg_notional_usd": avg_notional,
                    "window_minutes": SELL_CLUSTER_WINDOW_MINUTES,
                },
            ))
            self._already_fired[key] = ts

        return candidates
