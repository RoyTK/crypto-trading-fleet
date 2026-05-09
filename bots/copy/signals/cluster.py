"""Cluster Buy signal generator (stateful).

Locked Item #7 logic:
- Maintain a 15-min rolling buffer of buys per (chain, token)
- Trigger when ≥3 distinct wallets bought the same token in the window,
  EACH with ≥$5k notional
- Token validation: age <24h OR vol jumped >5× last hour
- Direction: always LONG
- Cluster size → position size band (handled in sizing.py)

State: in-memory deque keyed by (chain, token). Pruned on every evaluate().
Same _already_fired pattern as STRUCTURE's LiquidationCascadeDetector — we
don't re-emit a signal for the same (chain, token) while the prior one's
window is still active.

Build A simplification: token validation is OPTIONAL — if no token meta
provider is available, we still trigger on cluster size + per-wallet
notional. The token-meta gate becomes mandatory in Build B.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from time import time
from typing import Optional

from bots.copy.config import (
    CLUSTER_MIN_NOTIONAL_PER_WALLET_USD,
    CLUSTER_MIN_WALLETS,
    CLUSTER_TOKEN_MAX_AGE_HOURS,
    CLUSTER_VOL_JUMP_THRESHOLD,
    CLUSTER_WINDOW_MINUTES,
    EXIT_STOP_PCT,
    EXIT_TAKE_PROFIT_PCT,
    EXIT_TIMEOUT_HOURS,
)
from bots.copy.signals.base import SignalCandidate
from bots.copy.venue.helius_solana import WalletBuyEvent


WINDOW_SECONDS = CLUSTER_WINDOW_MINUTES * 60


@dataclass
class TokenMeta:
    """Optional metadata used to validate a token before emitting a signal."""
    age_hours: float = 0.0
    last_hour_vol_usd: float = 0.0
    prior_24h_avg_hourly_vol_usd: float = 0.0


@dataclass
class _TokenWindow:
    """Per (chain, token) rolling buffer of (wallet, ts_ms, notional_usd)."""
    buys: deque = field(default_factory=deque)


class ClusterDetector:
    """Stateful detector. Single instance held by the bot loop."""

    def __init__(self) -> None:
        self._windows: dict[tuple[str, str], _TokenWindow] = {}
        self._already_fired: dict[tuple[str, str], int] = {}

    def observe_buy(self, ev: WalletBuyEvent) -> None:
        """Push a wallet buy event into the rolling window."""
        if ev.notional_usd < CLUSTER_MIN_NOTIONAL_PER_WALLET_USD:
            return  # too small — can't contribute to cluster
        key = (ev.chain, ev.token_mint)
        w = self._windows.setdefault(key, _TokenWindow())
        w.buys.append((ev.wallet_address, ev.timestamp_ms, ev.notional_usd))

    def _prune(self, key: tuple[str, str], now_ms: int) -> None:
        cutoff = now_ms - WINDOW_SECONDS * 1000
        w = self._windows.get(key)
        if w is None:
            return
        while w.buys and w.buys[0][1] < cutoff:
            w.buys.popleft()

    def evaluate(
        self,
        token_meta: Optional[dict[tuple[str, str], TokenMeta]] = None,
        now_ms: Optional[int] = None,
    ) -> list[SignalCandidate]:
        """Return cluster signal candidates for any (chain, token) where
        the trigger condition is met right now.

        token_meta: optional per-(chain,token) age/volume info. If absent,
        the token-validation gate is skipped (Build A behavior).
        """
        ts = now_ms or int(time() * 1000)
        candidates: list[SignalCandidate] = []
        token_meta = token_meta or {}

        for key in list(self._windows.keys()):
            self._prune(key, ts)
            w = self._windows[key]
            if not w.buys:
                continue

            chain, token = key

            # Distinct wallets above the per-wallet notional floor
            distinct_wallets: dict[str, float] = {}
            for wallet, _ts_ms, notional in w.buys:
                distinct_wallets[wallet] = max(distinct_wallets.get(wallet, 0.0), notional)
            qualifying_notionals: dict[str, float] = {
                w_addr: n
                for w_addr, n in distinct_wallets.items()
                if n >= CLUSTER_MIN_NOTIONAL_PER_WALLET_USD
            }
            qualifying = list(qualifying_notionals.keys())
            cluster_size = len(qualifying)
            if cluster_size < CLUSTER_MIN_WALLETS:
                continue

            # Token-validation gate (skipped if no meta available)
            meta = token_meta.get(key)
            if meta is not None and not _token_passes(meta):
                continue

            # Suppress re-fire while prior signal's window is still active
            last = self._already_fired.get(key, 0)
            if (ts - last) < WINDOW_SECONDS * 1000:
                continue

            total_notional = sum(qualifying_notionals.values())
            avg_notional = total_notional / cluster_size

            candidates.append(SignalCandidate(
                signal_type="cluster_buy",
                asset=token,
                chain=chain,
                direction="long",
                cluster_size=cluster_size,
                stop_pct=EXIT_STOP_PCT,
                take_profit_pct=EXIT_TAKE_PROFIT_PCT,
                timeout_hours=EXIT_TIMEOUT_HOURS,
                payload={
                    "wallets": qualifying,
                    "wallet_notionals": qualifying_notionals,
                    "cluster_size": cluster_size,
                    "total_notional_usd": total_notional,
                    "avg_notional_usd": avg_notional,
                    "window_minutes": CLUSTER_WINDOW_MINUTES,
                    "token_meta_used": meta is not None,
                },
            ))
            self._already_fired[key] = ts

        return candidates


def _token_passes(meta: TokenMeta) -> bool:
    """Locked Item #7: token age <24h OR vol jumped >5× last hour."""
    if meta.age_hours < CLUSTER_TOKEN_MAX_AGE_HOURS:
        return True
    if meta.prior_24h_avg_hourly_vol_usd > 0:
        ratio = meta.last_hour_vol_usd / meta.prior_24h_avg_hourly_vol_usd
        if ratio >= CLUSTER_VOL_JUMP_THRESHOLD:
            return True
    return False
