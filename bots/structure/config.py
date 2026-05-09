"""STRUCTURE bot configuration.

Threshold and sizing constants are locked from Item #7 of the design agenda
(2026-04-26). Runtime knobs (intervals, paper capital, shadow %) come from
.env via pydantic-settings.

Do NOT change the locked thresholds during the paper window — anti-gaming
armor requires signal logic to be frozen. Tuning happens during shakedown
debug (before paper clock starts) and during quarterly ops review (after the
paper window).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Locked v0 thresholds (Item #7)
# ---------------------------------------------------------------------------

# Funding Fade — annualized rate triggers
FUNDING_SHORT_THRESHOLD_PCT = 50.0     # > +50% annualized → SHORT
FUNDING_LONG_THRESHOLD_PCT = -30.0     # < -30% annualized → LONG
FUNDING_EXIT_BAND_PCT = 10.0           # exit when |annualized| <= 10%
FUNDING_OI_FLOOR_USD = 10_000_000.0    # OI must be >= $10M
FUNDING_TOP_VOL_RANK = 20
FUNDING_SIZE_PCT_MIN = 5.0
FUNDING_SIZE_PCT_MAX = 10.0
FUNDING_LEVERAGE = 2.0
FUNDING_STOP_PCT = 5.0
FUNDING_TIMEOUT_HOURS = 24

# Liquidation Cascade — rolling 5-min window
LIQ_NOTIONAL_THRESHOLD_USD = 5_000_000.0
LIQ_PRICE_MOVE_THRESHOLD_PCT = 4.0
LIQ_WINDOW_MINUTES = 5
LIQ_TOP_VOL_RANK = 15
LIQ_SIZE_PCT_MIN = 5.0
LIQ_SIZE_PCT_MAX = 12.0
LIQ_LEVERAGE = 3.0
LIQ_STOP_PCT = 4.0
LIQ_TAKE_PROFIT_PCT = 3.0

# Whale Flip — per-whale poll
# Original Item #7 spec was $500k. Lowered to $250k 2026-05-09 after curation
# data showed 65% of active HL traders currently hold positions in the
# $250k-$500k range; $500k threshold made whale_flip a rare-event detector
# (0 qualifying flips in 9 days of operation). $250k preserves "real skin in
# the game" while expanding the addressable pool ~50x.
WHALE_FLIP_THRESHOLD_USD = 250_000.0
WHALE_MIN_HISTORICAL_WIN_RATE = 0.60
WHALE_LIST_TARGET_SIZE = 50          # curate up to this many; minimum 10 to ship Build A
WHALE_LIST_MIN_SIZE = 10
WHALE_SIZE_PCT_MIN = 4.0
WHALE_SIZE_PCT_MAX = 8.0
WHALE_LEVERAGE = 2.0
WHALE_STOP_PCT = 6.0
WHALE_TIMEOUT_HOURS = 48

# Per-bot caps (Item #1 + Item #4)
PER_TRADE_NOTIONAL_CAP_PCT = 15.0
ALLOCATION_CAP_SOLO_PCT = 60.0
ALLOCATION_CAP_MULTI_PCT = 50.0

# Hyperliquid public fee schedule (non-VIP, v0)
TAKER_FEE_PCT = 0.05
MAKER_FEE_PCT = 0.02


# ---------------------------------------------------------------------------
# Runtime configuration (env-driven)
# ---------------------------------------------------------------------------

class StructureSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Hyperliquid
    hyperliquid_api_url: str = Field(default="https://api.hyperliquid.xyz")
    hyperliquid_agent_private_key: str = Field(default="")
    hyperliquid_master_address: str = Field(default="")

    # STRUCTURE-specific
    structure_paper_capital_usd: float = Field(default=1000.0)
    structure_shadow_pct: float = Field(default=10.0)
    structure_dd_daily_pct: float = Field(default=15.0)
    structure_dd_weekly_pct: float = Field(default=30.0)
    structure_dd_total_pct: float = Field(default=45.0)
    structure_loop_interval_seconds: int = Field(default=5)
    structure_funding_poll_seconds: int = Field(default=30)
    structure_whale_poll_seconds: int = Field(default=60)


@lru_cache(maxsize=1)
def get_structure_settings() -> StructureSettings:
    return StructureSettings()


# ---------------------------------------------------------------------------
# Signal-type metadata used by sizing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SignalSpec:
    name: str
    size_pct_min: float
    size_pct_max: float
    leverage: float
    stop_pct: float


SIGNAL_SPECS: dict[str, SignalSpec] = {
    "funding_fade": SignalSpec(
        name="funding_fade",
        size_pct_min=FUNDING_SIZE_PCT_MIN,
        size_pct_max=FUNDING_SIZE_PCT_MAX,
        leverage=FUNDING_LEVERAGE,
        stop_pct=FUNDING_STOP_PCT,
    ),
    "liquidation_cascade": SignalSpec(
        name="liquidation_cascade",
        size_pct_min=LIQ_SIZE_PCT_MIN,
        size_pct_max=LIQ_SIZE_PCT_MAX,
        leverage=LIQ_LEVERAGE,
        stop_pct=LIQ_STOP_PCT,
    ),
    "whale_flip": SignalSpec(
        name="whale_flip",
        size_pct_min=WHALE_SIZE_PCT_MIN,
        size_pct_max=WHALE_SIZE_PCT_MAX,
        leverage=WHALE_LEVERAGE,
        stop_pct=WHALE_STOP_PCT,
    ),
}
