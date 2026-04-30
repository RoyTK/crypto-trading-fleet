"""Whale Flip signal generator.

Logic (locked v0):
- Poll user_state for each whale in whale_list.json every 60s
- For each whale × asset, track prior signed notional position
- Detect flips: prior direction != current AND |prior| >= $500k
- Emit a SignalCandidate following the whale's NEW direction
- Sizing: 4-8% at 2x leverage; stop 6%; 48h timeout

State: stateful — tracks prior poll positions per (whale_address, asset).
The bot loop calls observe_positions() with each whale's latest user_state
output, then evaluate() to extract any flips.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bots.structure.config import (
    WHALE_FLIP_THRESHOLD_USD,
    WHALE_STOP_PCT,
    WHALE_TIMEOUT_HOURS,
)
from bots.structure.signals.base import SignalCandidate
from bots.structure.venue import WhalePosition


@dataclass
class _PriorPosition:
    notional_usd: float           # signed; +long, -short
    last_seen_ts_ms: int


class WhaleFlipDetector:
    """Holds the prior-poll position state per whale × asset."""

    def __init__(self) -> None:
        # key = (whale_address, asset)
        self._prior: dict[tuple[str, str], _PriorPosition] = {}
        self._pending_flips: list[SignalCandidate] = []

    def observe_positions(
        self,
        whale_address: str,
        positions: list[WhalePosition],
        now_ts_ms: int,
        whale_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Compare against prior state; queue flips for evaluate() to drain."""
        seen_assets = set()
        for pos in positions:
            seen_assets.add(pos.asset)
            key = (whale_address, pos.asset)
            prior = self._prior.get(key)

            # Only consider entries with enough notional to be meaningful
            if abs(pos.notional_usd) < WHALE_FLIP_THRESHOLD_USD and (
                prior is None or abs(prior.notional_usd) < WHALE_FLIP_THRESHOLD_USD
            ):
                # Update record so we don't false-trigger later
                self._prior[key] = _PriorPosition(
                    notional_usd=pos.notional_usd,
                    last_seen_ts_ms=now_ts_ms,
                )
                continue

            if prior is not None and abs(prior.notional_usd) >= WHALE_FLIP_THRESHOLD_USD:
                prior_sign = 1 if prior.notional_usd > 0 else -1
                cur_sign = 1 if pos.notional_usd > 0 else (-1 if pos.notional_usd < 0 else 0)
                if cur_sign != 0 and cur_sign != prior_sign:
                    direction = "long" if cur_sign > 0 else "short"
                    flip_size_usd = abs(prior.notional_usd) + abs(pos.notional_usd)
                    conviction = min(
                        1.0, flip_size_usd / (WHALE_FLIP_THRESHOLD_USD * 4.0)
                    )
                    self._pending_flips.append(SignalCandidate(
                        signal_type="whale_flip",
                        asset=pos.asset,
                        direction=direction,
                        conviction=max(0.4, conviction),
                        stop_pct=WHALE_STOP_PCT,
                        timeout_hours=WHALE_TIMEOUT_HOURS,
                        payload={
                            "whale_address": whale_address,
                            "whale_metadata": whale_metadata or {},
                            "prior_notional_usd": prior.notional_usd,
                            "current_notional_usd": pos.notional_usd,
                            "flip_magnitude_usd": flip_size_usd,
                        },
                    ))

            self._prior[key] = _PriorPosition(
                notional_usd=pos.notional_usd,
                last_seen_ts_ms=now_ts_ms,
            )

        # If a whale closed a previously-tracked position entirely, mark it zero
        # (no flip; just exit). We DO NOT trigger a signal on close-to-zero —
        # locked design says flip means direction change, not exit.
        for key in list(self._prior.keys()):
            if key[0] != whale_address:
                continue
            if key[1] not in seen_assets:
                self._prior[key] = _PriorPosition(
                    notional_usd=0.0,
                    last_seen_ts_ms=now_ts_ms,
                )

    def evaluate(self) -> list[SignalCandidate]:
        """Drain any flip candidates queued since the last call."""
        out = self._pending_flips
        self._pending_flips = []
        return out
