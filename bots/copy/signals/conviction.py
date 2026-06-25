"""Conviction signal generator — single elite-wallet trigger (stateful).

Parallel to ClusterDetector, but fires on ONE roster ("conviction") wallet's
buy — no cluster required. The conviction strategy runs alongside the cluster
strategy with its own paper bankroll + isolated metrics so its single-wallet
edge can be measured independently (see project plan 2026-06-24).

Design notes:
- Roster = wallet_pool WHERE conviction = true, pushed in via set_wallets()
  (refreshed whenever the bot reloads its wallet pool). The detector itself is
  pure/stateful and DB-free so it's unit-testable in isolation.
- Independence from cluster is total: conviction and cluster may both fire on
  (and hold) the same token at once. An overlap is a STRONGER signal, by design
  — both buy, tracked separately. The only suppression here is per-(chain,token)
  re-fire damping (mirrors the cluster 15-min window); the "don't open a second
  conviction position in a token we already hold" guard is enforced at consume
  time via has_open_position(..., strategy='conviction').
"""
from __future__ import annotations

from time import time
from typing import Iterable, Optional

from bots.copy.config import (
    CLUSTER_WINDOW_MINUTES,
    EXIT_STOP_PCT,
    EXIT_TAKE_PROFIT_PCT,
    EXIT_TIMEOUT_HOURS,
    get_copy_settings,
)
from bots.copy.signals.base import SignalCandidate
from bots.copy.venue.helius_solana import WalletBuyEvent


# Re-fire suppression window for the SAME (chain, token). Mirrors the cluster
# 15-min window: prevents emitting a fresh candidate every time a roster wallet
# re-buys the same token in quick succession. Open-position dedup is separate.
SUPPRESS_SECONDS = CLUSTER_WINDOW_MINUTES * 60


class ConvictionDetector:
    """Stateful single-wallet detector. One instance per bot loop."""

    def __init__(
        self,
        wallets: Optional[Iterable[str]] = None,
        min_notional_usd: Optional[float] = None,
    ) -> None:
        self._wallets: set[str] = set(wallets or ())
        self._min_notional_usd = (
            float(min_notional_usd)
            if min_notional_usd is not None
            else float(get_copy_settings().copy_conviction_min_notional_usd)
        )
        # key (chain, token) -> (trigger_wallet, ts_ms, notional_usd)
        self._pending: dict[tuple[str, str], tuple[str, int, float]] = {}
        self._already_fired: dict[tuple[str, str], int] = {}

    def set_wallets(self, wallets: Iterable[str]) -> None:
        """Refresh the conviction roster (called on wallet-pool reload)."""
        self._wallets = {w for w in (wallets or ()) if w}

    @property
    def wallet_count(self) -> int:
        return len(self._wallets)

    def observe_buy(self, ev: WalletBuyEvent) -> None:
        """Record a buy from a roster wallet as a pending trigger."""
        if ev.wallet_address not in self._wallets:
            return
        if ev.notional_usd < self._min_notional_usd:
            return
        key = (ev.chain, ev.token_mint)
        # Keep the most-recent triggering buy for this token.
        self._pending[key] = (ev.wallet_address, ev.timestamp_ms, ev.notional_usd)

    def evaluate(self, now_ms: Optional[int] = None) -> list[SignalCandidate]:
        """Drain pending triggers into conviction_buy candidates.

        Applies per-(chain,token) re-fire suppression so the same token can't
        re-emit within the window.
        """
        ts = now_ms or int(time() * 1000)
        out: list[SignalCandidate] = []
        for key in list(self._pending.keys()):
            wallet, _ts_ms, notional = self._pending.pop(key)
            last = self._already_fired.get(key, 0)
            if (ts - last) < SUPPRESS_SECONDS * 1000:
                continue
            chain, token = key
            out.append(SignalCandidate(
                signal_type="conviction_buy",
                asset=token,
                chain=chain,
                direction="long",
                cluster_size=1,
                stop_pct=EXIT_STOP_PCT,
                take_profit_pct=EXIT_TAKE_PROFIT_PCT,
                timeout_hours=EXIT_TIMEOUT_HOURS,
                payload={
                    "strategy": "conviction",
                    "trigger_wallet": wallet,
                    "trigger_notional_usd": notional,
                    # Carried so downstream wallet-list helpers keep working;
                    # the single trigger wallet IS the cohort here.
                    "wallets": [wallet],
                },
            ))
            self._already_fired[key] = ts
        return out
