"""Conviction signal generator — single elite-wallet accumulation trigger.

Parallel to ClusterDetector, but fires on ONE roster ("conviction") wallet
ACCUMULATING a token — no cluster required. The conviction strategy runs
alongside the cluster strategy with its own paper bankroll + isolated metrics
so its single-wallet edge can be measured independently.

Trigger model (2026-06-25, replaces the old single-buy ≥ $1k floor): Birdeye
analysis showed these wallets build winners from many tiny clips (CyaE1Vx ≈ 78%
fixed ~$16 buys), so a single-buy floor is wrong — too low fires on meaningless
nibbles, too high misses the accumulation. Instead we SUM a wallet's buys per
token over a rolling window and fire when committed >= a threshold:

- Per (chain, token, wallet): keep a rolling window of the wallet's buys (above a
  dust floor) AND its sells. Each evaluate(): prune both to the window, sum.
- Fire iff buys_sum >= threshold AND sells_sum <= sell_holdoff. The sell hold-off
  (Roy, 2026-06-25): if the wallet is ALSO selling the token in the window it's
  churning/distributing, not cleanly accumulating — hold off on the buy.
- A single large buy crosses the threshold instantly, so snipers still fire with
  no lateness — this generalizes the old single-buy mechanism.
- On fire we reset that key's buy window (we've acted on this build); fresh
  accumulation is required to fire again. The "don't open a second conviction
  position while already holding" guard is enforced at consume time via
  has_open_position(..., strategy='conviction').

The detector is pure/stateful and DB-free (unit-testable in isolation). Roster =
wallet_pool WHERE conviction = true, pushed in via set_wallets() on pool reload.
Independence from cluster is total: both may fire on / hold the same token.
"""
from __future__ import annotations

from collections import deque
from time import time
from typing import Iterable, Optional

from bots.copy.config import (
    EXIT_STOP_PCT,
    EXIT_TAKE_PROFIT_PCT,
    EXIT_TIMEOUT_HOURS,
    get_copy_settings,
)
from bots.copy.signals.base import SignalCandidate
from bots.copy.venue.helius_solana import WalletBuyEvent, WalletSellEvent


_Key = tuple[str, str, str]  # (chain, token_mint, wallet_address)


class ConvictionDetector:
    """Stateful single-wallet accumulation detector. One instance per bot loop."""

    def __init__(
        self,
        wallets: Optional[Iterable[str]] = None,
        *,
        dust_floor_usd: Optional[float] = None,
        threshold_usd: Optional[float] = None,
        window_minutes: Optional[float] = None,
        sell_holdoff_usd: Optional[float] = None,
    ) -> None:
        s = get_copy_settings()
        self._wallets: set[str] = {w for w in (wallets or ()) if w}
        self._dust = float(
            dust_floor_usd if dust_floor_usd is not None
            else s.copy_conviction_dust_floor_usd
        )
        self._threshold = float(
            threshold_usd if threshold_usd is not None
            else s.copy_conviction_accumulation_threshold_usd
        )
        win_min = (
            window_minutes if window_minutes is not None
            else s.copy_conviction_accumulation_window_minutes
        )
        self._window_ms = int(float(win_min) * 60 * 1000)
        self._sell_holdoff = float(
            sell_holdoff_usd if sell_holdoff_usd is not None
            else s.copy_conviction_sell_holdoff_usd
        )
        # key -> deque[(ts_ms, notional_usd)]
        self._buys: dict[_Key, deque] = {}
        self._sells: dict[_Key, deque] = {}

    def set_wallets(self, wallets: Iterable[str]) -> None:
        """Refresh the conviction roster (called on wallet-pool reload)."""
        self._wallets = {w for w in (wallets or ()) if w}

    @property
    def wallet_count(self) -> int:
        return len(self._wallets)

    def observe_buy(self, ev: WalletBuyEvent) -> None:
        """Record a roster wallet's buy (above the dust floor) into its window."""
        if ev.wallet_address not in self._wallets or ev.notional_usd < self._dust:
            return
        key = (ev.chain, ev.token_mint, ev.wallet_address)
        self._buys.setdefault(key, deque()).append((ev.timestamp_ms, float(ev.notional_usd)))

    def observe_sell(self, ev: WalletSellEvent) -> None:
        """Record a roster wallet's sell — used to hold off the buy trigger when
        the wallet is also distributing the token in the window."""
        if ev.wallet_address not in self._wallets or ev.notional_usd < self._dust:
            return
        key = (ev.chain, ev.token_mint, ev.wallet_address)
        self._sells.setdefault(key, deque()).append((ev.timestamp_ms, float(ev.notional_usd)))

    @staticmethod
    def _prune(dq: deque, cutoff: int) -> None:
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def evaluate(self, now_ms: Optional[int] = None) -> list[SignalCandidate]:
        """Fire a conviction_buy for any (chain, token, wallet) whose windowed buys
        reach the threshold while not also selling (above the hold-off tolerance)."""
        ts = now_ms or int(time() * 1000)
        cutoff = ts - self._window_ms

        # Prune + GC the sells map (covers tokens a wallet only sold, never bought).
        for key in list(self._sells.keys()):
            self._prune(self._sells[key], cutoff)
            if not self._sells[key]:
                del self._sells[key]

        out: list[SignalCandidate] = []
        for key in list(self._buys.keys()):
            bdq = self._buys[key]
            self._prune(bdq, cutoff)
            if not bdq:
                del self._buys[key]
                continue
            buys_sum = sum(n for _, n in bdq)
            if buys_sum < self._threshold:
                continue
            sdq = self._sells.get(key)
            sells_sum = sum(n for _, n in sdq) if sdq else 0.0
            if sells_sum > self._sell_holdoff:
                continue  # wallet is also selling this token — hold off

            chain, token, wallet = key
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
                    "accumulated_usd": round(buys_sum, 2),
                    "n_buys": len(bdq),
                    "window_sells_usd": round(sells_sum, 2),
                    "window_minutes": self._window_ms // 60000,
                    # Carried so downstream wallet-list helpers keep working.
                    "wallets": [wallet],
                },
            ))
            # Acted on this build — reset the buy window for this key. Fresh
            # accumulation (>= threshold of new buys) is required to fire again.
            self._buys.pop(key, None)
        return out
