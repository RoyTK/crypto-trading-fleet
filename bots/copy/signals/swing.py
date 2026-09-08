"""Swing-copy signal generator — multi-day single-wallet follow-in trigger.

Sibling of ConvictionDetector, but tuned for MULTI-DAY holders (see the 2026-08
follow-in/follow-out backtest, reports/swing_copy_validation_2026-08-26/): on a
multi-day horizon our ~7s-2min latency is noise, so we can follow a wallet IN
early and mirror its net-distribution exit OUT. Conviction died on fast spikes
where that's impossible.

Trigger model: sum a roster ("swing") wallet's buys AND sells per token over a
rolling window; fire when NET buys (buys − sells) >= threshold. This differs from
conviction two ways: (1) it's a NET trigger (a wallet that buys→small-sell→buys
while accumulating still fires — we do NOT hold off on suppression sells, since
swing WANTS to ride the build); (2) min_buys defaults to 1 and span to 0 — enter
EARLY, there's no lateness penalty on a multi-day build.

On fire we reset that key's buy+sell windows (acted on this build; fresh net
accumulation is required to fire again). The exit is NOT here — it's the net-flow
follow-out in main._follow_swing_out (exit when the wallet net-distributes >= a
fraction of its build). Pure/stateful/DB-free; roster = wallet_pool.swing=true.
"""
from __future__ import annotations

from collections import deque
from time import time
from typing import Iterable, Optional

from bots.copy.config import EXIT_TAKE_PROFIT_PCT, get_copy_settings
from bots.copy.signals.base import SignalCandidate
from bots.copy.venue.helius_solana import WalletBuyEvent, WalletSellEvent


_Key = tuple[str, str, str]  # (chain, token_mint, wallet_address)


class SwingDetector:
    """Stateful single-wallet NET-accumulation detector for swing-copy."""

    def __init__(
        self,
        wallets: Optional[Iterable[str]] = None,
        *,
        dust_floor_usd: Optional[float] = None,
        threshold_usd: Optional[float] = None,
        window_minutes: Optional[float] = None,
        min_buys: Optional[int] = None,
        min_accumulation_span_seconds: Optional[float] = None,
    ) -> None:
        s = get_copy_settings()
        self._wallets: set[str] = {w for w in (wallets or ()) if w}
        self._dust = float(
            dust_floor_usd if dust_floor_usd is not None
            else s.copy_swing_dust_floor_usd
        )
        self._threshold = float(
            threshold_usd if threshold_usd is not None
            else s.copy_swing_accumulation_threshold_usd
        )
        win_min = (
            window_minutes if window_minutes is not None
            else s.copy_swing_accumulation_window_minutes
        )
        self._window_ms = int(float(win_min) * 60 * 1000)
        self._min_buys = int(
            min_buys if min_buys is not None else s.copy_swing_min_buys
        )
        self._min_span_ms = int(
            (min_accumulation_span_seconds
             if min_accumulation_span_seconds is not None
             else s.copy_swing_min_accumulation_span_seconds) * 1000
        )
        self._stop_pct = float(s.copy_swing_stop_pct)
        self._timeout_hours = int(s.copy_swing_timeout_hours)
        # key -> deque[(ts_ms, notional_usd)]
        self._buys: dict[_Key, deque] = {}
        self._sells: dict[_Key, deque] = {}

    def set_wallets(self, wallets: Iterable[str]) -> None:
        self._wallets = {w for w in (wallets or ()) if w}

    @property
    def wallet_count(self) -> int:
        return len(self._wallets)

    def observe_buy(self, ev: WalletBuyEvent) -> None:
        if ev.wallet_address not in self._wallets or ev.notional_usd < self._dust:
            return
        key = (ev.chain, ev.token_mint, ev.wallet_address)
        self._buys.setdefault(key, deque()).append((ev.timestamp_ms, float(ev.notional_usd)))

    def observe_sell(self, ev: WalletSellEvent) -> None:
        """Record a roster wallet's sell so the NET trigger nets it out (suppression
        sells reduce net accumulation but don't hold us off like conviction)."""
        if ev.wallet_address not in self._wallets or ev.notional_usd < self._dust:
            return
        key = (ev.chain, ev.token_mint, ev.wallet_address)
        self._sells.setdefault(key, deque()).append((ev.timestamp_ms, float(ev.notional_usd)))

    @staticmethod
    def _prune(dq: deque, cutoff: int) -> None:
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def evaluate(self, now_ms: Optional[int] = None) -> list[SignalCandidate]:
        """Fire a swing_buy for any (chain, token, wallet) whose windowed NET buys
        (buys − sells) reach the threshold with enough distinct buys / span."""
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
            sdq = self._sells.get(key)
            sells_sum = sum(n for _, n in sdq) if sdq else 0.0
            net_sum = buys_sum - sells_sum
            if net_sum < self._threshold:
                continue
            if len(bdq) < self._min_buys:
                continue
            if (bdq[-1][0] - bdq[0][0]) < self._min_span_ms:
                continue

            chain, token, wallet = key
            accumulation_seconds = round((bdq[-1][0] - bdq[0][0]) / 1000.0, 1)
            out.append(SignalCandidate(
                signal_type="swing_buy",
                asset=token,
                chain=chain,
                direction="long",
                cluster_size=1,
                stop_pct=self._stop_pct,
                take_profit_pct=EXIT_TAKE_PROFIT_PCT,
                # Long hold cap — swing rides multi-day; the net-flow follow-out +
                # wide catastrophe stop + rug backstop govern exits, not the clock.
                timeout_hours=self._timeout_hours,
                payload={
                    "strategy": "swing",
                    "trigger_wallet": wallet,
                    "accumulated_usd": round(net_sum, 2),
                    "n_buys": len(bdq),
                    "accumulation_seconds": accumulation_seconds,
                    "window_sells_usd": round(sells_sum, 2),
                    "window_minutes": self._window_ms // 60000,
                    "wallets": [wallet],
                },
            ))
            # Acted on this build — reset both windows for this key.
            self._buys.pop(key, None)
            self._sells.pop(key, None)
        return out
