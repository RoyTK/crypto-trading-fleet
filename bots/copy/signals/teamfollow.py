"""Team-follow signal generator (stateful) — EXPERIMENT (2026-07-01).

Fires when >= N distinct members of the SAME known co-buy TEAM buy the same token
within a rolling window. Teams come from the Dune 90d corpus team-finder
(bots/copy/teamfollow_roster.json): wallet groups that repeatedly co-buy the same
runners tightly (overlap >= 0.35). This runs as its OWN isolated strategy
('teamfollow') with its own paper bankroll + metrics, alongside cluster/conviction.

Why this exists: the DB build (see project_cluster_database_build memory) showed the
RAW 3-wallet cluster signal is a spray (97% duds) BUT that when a *specific* recurring
team co-buys AND we apply a real liquidity floor + cluster's ladder/trailing exits,
the strategy is strongly +EV (liquidity determines whether the stop can fill). This
detector is the LIVE forward test of that: signal -> (entry liq floor, in main loop)
-> buy -> cluster exit stack. Team selection barely matters post-floor, so we seed the
FULL 129-team roster and prune from live data.

Mechanics mirror ClusterDetector but the qualifying condition is team-scoped: instead
of ">=3 distinct wallets," it is ">= min_members distinct wallets sharing a team_id."
The entry LIQUIDITY FLOOR (the real quality gate) is applied in the main loop at entry,
not here. Per-wallet buy floor here is a small dust filter only.
"""
from __future__ import annotations

from collections import deque
from time import time
from typing import Iterable, Mapping, Optional

from bots.copy.config import (
    EXIT_STOP_PCT,
    EXIT_TAKE_PROFIT_PCT,
    EXIT_TIMEOUT_HOURS,
    get_copy_settings,
)
from bots.copy.signals.base import SignalCandidate
from bots.copy.venue.helius_solana import WalletBuyEvent


class TeamFollowDetector:
    """Stateful team-scoped co-buy detector. Single instance held by the bot loop."""

    def __init__(
        self,
        roster: Optional[Mapping[str, int]] = None,
        *,
        min_members: Optional[int] = None,
        window_minutes: Optional[float] = None,
        dust_floor_usd: Optional[float] = None,
    ) -> None:
        s = get_copy_settings()
        # wallet -> team_id
        self._roster: dict[str, int] = {w: int(t) for w, t in (roster or {}).items() if w}
        self._min = int(
            min_members if min_members is not None
            else s.copy_teamfollow_min_members
        )
        win_min = (
            window_minutes if window_minutes is not None
            else s.copy_teamfollow_window_minutes
        )
        self._window_ms = int(float(win_min) * 60 * 1000)
        self._dust = float(
            dust_floor_usd if dust_floor_usd is not None
            else s.copy_teamfollow_dust_floor_usd
        )
        # (chain, token) -> deque[(wallet, ts_ms, notional, team_id)]
        self._windows: dict[tuple[str, str], deque] = {}
        # suppress re-fire per (chain, token, team_id) while prior window still active
        self._already_fired: dict[tuple[str, str, int], int] = {}

    def set_roster(self, roster: Mapping[str, int]) -> None:
        """Refresh the wallet->team_id roster (called on reload)."""
        self._roster = {w: int(t) for w, t in (roster or {}).items() if w}

    @property
    def wallet_count(self) -> int:
        return len(self._roster)

    @property
    def team_count(self) -> int:
        return len(set(self._roster.values()))

    def observe_buy(self, ev: WalletBuyEvent) -> None:
        """Push a roster wallet's buy (above dust) into the token window."""
        team = self._roster.get(ev.wallet_address)
        if team is None or ev.notional_usd < self._dust:
            return
        key = (ev.chain, ev.token_mint)
        self._windows.setdefault(key, deque()).append(
            (ev.wallet_address, ev.timestamp_ms, float(ev.notional_usd), team)
        )

    def _prune(self, dq: deque, cutoff: int) -> None:
        while dq and dq[0][1] < cutoff:
            dq.popleft()

    def evaluate(self, now_ms: Optional[int] = None) -> list[SignalCandidate]:
        """Fire a teamfollow_buy for any (chain, token) where >= min_members distinct
        members of one team co-bought within the window."""
        ts = now_ms or int(time() * 1000)
        cutoff = ts - self._window_ms
        out: list[SignalCandidate] = []

        for key in list(self._windows.keys()):
            dq = self._windows[key]
            self._prune(dq, cutoff)
            if not dq:
                del self._windows[key]
                continue
            chain, token = key

            # group distinct wallets by team; keep each wallet's max notional
            by_team: dict[int, dict[str, float]] = {}
            for wallet, _tsm, notional, team in dq:
                d = by_team.setdefault(team, {})
                d[wallet] = max(d.get(wallet, 0.0), notional)

            for team, wallets in by_team.items():
                if len(wallets) < self._min:
                    continue
                fkey = (chain, token, team)
                last = self._already_fired.get(fkey)
                if last is not None and (ts - last) < self._window_ms:
                    continue  # already fired for this team+token this window
                total = sum(wallets.values())
                out.append(SignalCandidate(
                    signal_type="teamfollow_buy",
                    asset=token,
                    chain=chain,
                    direction="long",
                    cluster_size=len(wallets),
                    stop_pct=EXIT_STOP_PCT,
                    take_profit_pct=EXIT_TAKE_PROFIT_PCT,
                    timeout_hours=EXIT_TIMEOUT_HOURS,
                    payload={
                        "strategy": "teamfollow",
                        "team_id": team,
                        "wallets": sorted(wallets.keys()),
                        "wallet_notionals": wallets,
                        "cluster_size": len(wallets),
                        "total_notional_usd": round(total, 2),
                        "window_minutes": self._window_ms // 60000,
                    },
                ))
                self._already_fired[fkey] = ts

        return out
