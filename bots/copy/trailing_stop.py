"""Pure-function trailing-stop machinery for COPY's exit ladder.

Shared between _manage_open_positions (paper) and _manage_open_real_trades
(shadow/live). Kept here as a pure function so unit tests don't need the
DB / venue / aiohttp surface area — just compute the right exit reason
given (entry, current, peak, params).

Behavior (per brainstorm 2026-05-30):

1. Hard cap: current_pct >= EXIT_TRAILING_HARD_CAP_PCT (default 200%) →
   force-close at "trailing_hard_cap". Memecoins frequently 5-10x then
   collapse fast; the cap locks in a guaranteed 3x without trying to
   time the inevitable dump.

2. Static stop: current_pct <= -EXIT_STOP_PCT (default -8%) → "stop".
   Downside safety net during the pre-activation phase.

3. Trailing active: once peak_pct >= EXIT_TRAILING_ACTIVATION_PCT (20%),
   the trailing stop overrides the static TP. Stop level is
   peak_pct - EXIT_TRAILING_STOP_PCT (default 25). Position closes at
   "trailing_stop" if current_pct falls below.

4. Static TP fallback: current_pct >= EXIT_TAKE_PROFIT_PCT (30%) → "tp".
   Only reachable if peak somehow skipped over activation (gap-up).

5. Timeout: caller decides whether to fire — this function only knows
   about the price-driven exits.

Returns (new_peak_pct, exit_reason_or_None). Caller is responsible for:
- Persisting new_peak_pct via update_trade_peak_pct
- Acting on exit_reason (synthesize fill, write close row, etc.)
- Firing timeout when applicable

Direction support: long-only for v0. Short positions (not currently
supported by COPY on Solana) would invert the sign logic — defer until
shorts are actually wired.
"""
from __future__ import annotations

from typing import Optional

from bots.copy.config import (
    EXIT_STOP_PCT,
    EXIT_TAKE_PROFIT_PCT,
    EXIT_TRAILING_ACTIVATION_PCT,
    EXIT_TRAILING_HARD_CAP_PCT,
    EXIT_TRAILING_STOP_PCT,
)


def evaluate_trailing_exit(
    *,
    entry_price: float,
    current_price: float,
    stored_peak_pct: Optional[float],
    leverage: float = 1.0,
    direction: str = "long",
    stop_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
    activation_pct: float = EXIT_TRAILING_ACTIVATION_PCT,
    trailing_drop_pct: float = EXIT_TRAILING_STOP_PCT,
    hard_cap_pct: float = EXIT_TRAILING_HARD_CAP_PCT,
) -> tuple[float, Optional[str]]:
    """Decide whether to exit a position given the current price.

    Returns (new_peak_pct, exit_reason_or_None).

    - stop_pct / take_profit_pct default to the global EXIT_STOP_PCT and
      EXIT_TAKE_PROFIT_PCT but can be overridden per-trade (the existing
      sim_metadata carries the values at entry time).
    - leverage scales the equity-side move; for COPY's spot DEX trades
      it's 1.0, but the math is leverage-aware for future-proofing.
    - direction='short' is unsupported in v0; called only with 'long' in
      current code. Falls through to "stop" with negated math (defensive
      so any future short flow doesn't silently break).
    """
    if entry_price <= 0:
        return (stored_peak_pct or 0.0, None)

    pct_move = (current_price - entry_price) / entry_price * 100.0
    if direction == "short":
        pct_move = -pct_move
    equity_pct = pct_move * leverage

    # Update peak monotonically. Defaults to 0 — pre-activation moves
    # below entry don't count as "peak".
    base_peak = stored_peak_pct if stored_peak_pct is not None else 0.0
    new_peak_pct = max(base_peak, equity_pct)

    # 1. Hard cap — guarantees we exit at 3x even if the move continues
    if equity_pct >= hard_cap_pct:
        return (new_peak_pct, "trailing_hard_cap")

    # 2. Static stop — downside protection (only applies if explicit)
    if stop_pct is not None and equity_pct <= -float(stop_pct):
        return (new_peak_pct, "stop")

    # 3. Trailing stop — active once peak crosses activation threshold
    if new_peak_pct >= activation_pct:
        trailing_stop_level = new_peak_pct - trailing_drop_pct
        if equity_pct <= trailing_stop_level:
            return (new_peak_pct, "trailing_stop")
        # Else: hold; trailing hasn't hit yet
        return (new_peak_pct, None)

    # 4. Static TP fallback — only reachable if activation was skipped
    if take_profit_pct is not None and equity_pct >= float(take_profit_pct):
        return (new_peak_pct, "tp")

    return (new_peak_pct, None)
