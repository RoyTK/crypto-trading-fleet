"""Selector-momentum signal (2026-10-01) — forward paper test of the Phase-2 study's one lead.

Study (reports/selector_study_2026-09-29/FINDINGS.md, exit replay): when a SELECTOR-class wallet
(wallet_style.cls = 'SELECTOR') makes the FIRST tracked selector buy of a token that is already
>= 10x off its 24h low, entering within ~1 minute with the fleet's standard exits was positive in
backtest (n=86, data-mined; gone at a 15-min entry). This strategy tests it forward, on paper.

This detector is the cheap, synchronous part: it watches the buys stream and queues a candidate
for every buy by a selector wallet on a token it has not queued before (per process). The
expensive checks (was this really the first selector buy? run-up from the 24h low) run in
main._consume_selmom_candidate, off the event loop's hot path.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Iterable, Optional

from bots.copy.config import EXIT_STOP_PCT, EXIT_TAKE_PROFIT_PCT, EXIT_TIMEOUT_HOURS, get_copy_settings
from bots.copy.signals.base import SignalCandidate
from bots.copy.venue.helius_solana import WalletBuyEvent

_SEEN_MAX = 50_000  # bounded per-process memory of tokens already queued


class SelectorMomentumDetector:
    def __init__(self, selectors: Optional[Iterable[str]] = None) -> None:
        self._selectors: set[str] = set(selectors or ())
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._queue: list[SignalCandidate] = []

    def set_selectors(self, wallets: Iterable[str]) -> None:
        self._selectors = set(wallets)

    @property
    def selector_count(self) -> int:
        return len(self._selectors)

    def observe_buy(self, ev: WalletBuyEvent) -> None:
        if ev.chain != "solana" or ev.wallet_address not in self._selectors:
            return
        if ev.notional_usd < get_copy_settings().copy_selmom_min_buy_usd:
            return
        if ev.token_mint in self._seen:
            return
        self._seen[ev.token_mint] = None
        if len(self._seen) > _SEEN_MAX:
            self._seen.popitem(last=False)
        cs = get_copy_settings()
        self._queue.append(SignalCandidate(
            signal_type="selmom_buy", asset=ev.token_mint, chain="solana", direction="long",
            cluster_size=1,
            payload={"trigger_wallet": ev.wallet_address, "trigger_buy_usd": ev.notional_usd,
                     "trigger_ts_ms": ev.timestamp_ms, "trigger_tx": ev.tx_signature},
            take_profit_pct=EXIT_TAKE_PROFIT_PCT,
            stop_pct=cs.copy_selmom_stop_pct if cs.copy_selmom_stop_pct > 0 else EXIT_STOP_PCT,
            timeout_hours=cs.copy_selmom_timeout_hours or EXIT_TIMEOUT_HOURS,
        ))

    def evaluate(self) -> list[SignalCandidate]:
        out, self._queue = self._queue, []
        return out
