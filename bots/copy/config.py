"""COPY bot configuration.

Threshold and sizing constants are locked from Item #7 of the design agenda.
Runtime knobs come from .env via pydantic-settings.

Do NOT change locked thresholds during the paper window — anti-gaming armor
requires signal logic to be frozen. Tuning happens during shakedown debug
(before paper clock starts) and during quarterly ops review.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Locked v0 thresholds (Item #7)
# ---------------------------------------------------------------------------

# Cluster trigger
CLUSTER_MIN_WALLETS = 3                    # ≥ 3 wallets = signal
CLUSTER_WINDOW_MINUTES = 15                # ±15 min cluster window
CLUSTER_MIN_NOTIONAL_PER_WALLET_USD = 5_000.0
CLUSTER_TOKEN_MAX_AGE_HOURS = 24           # token age <24h OR vol jumped >5×
CLUSTER_VOL_JUMP_THRESHOLD = 5.0           # last-hour vol vs prior 24h avg

# Cluster size → position % of paper capital (locked)
CLUSTER_SIZE_TO_PCT: dict[int, float] = {
    3: 4.0,
    4: 6.0,
    5: 6.0,
    # 6+ → 8%
}
CLUSTER_SIZE_LARGE_PCT = 8.0

# Per-trade cap
PER_TRADE_NOTIONAL_CAP_PCT = 8.0

# Allocation cap
ALLOCATION_CAP_PCT = 50.0

# Wallet curation criteria
WALLET_MIN_PNL_USD = 50_000.0
WALLET_MIN_WIN_RATE = 0.55
WALLET_MIN_HOLD_MINUTES = 30
WALLET_MAX_HOLD_DAYS = 7
WALLET_MIN_TRADES_90D = 20
WALLET_MIN_AGE_DAYS = 60
WALLET_POOL_TARGET_MIN = 200
WALLET_POOL_TARGET_MAX = 300

# Wallet auto-prune
WALLET_PRUNE_INACTIVE_DAYS = 60

# Per-bot exit logic
EXIT_STOP_PCT = 8.0                        # software stop (DEX has no native server-side stops)
EXIT_TAKE_PROFIT_PCT = 30.0                # quick TP for cluster signals
EXIT_TIMEOUT_HOURS = 12                    # cluster signals decay fast


# ---------------------------------------------------------------------------
# Runtime configuration (env-driven)
# ---------------------------------------------------------------------------

class CopySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Helius
    helius_api_key: str = Field(default="")
    helius_rpc_url: str = Field(default="https://mainnet.helius-rpc.com/")

    # Cielo
    cielo_api_key: str = Field(default="")
    cielo_api_base: str = Field(default="https://feed-api.cielo.finance/api/v1")

    # DEX quoters
    jupiter_quote_url: str = Field(default="https://quote-api.jup.ag/v6/quote")
    zeroex_api_base: str = Field(default="https://api.0x.org")

    # COPY-specific
    copy_paper_capital_usd: float = Field(default=1000.0)
    copy_shadow_pct: float = Field(default=10.0)
    copy_dd_daily_pct: float = Field(default=12.0)
    copy_dd_weekly_pct: float = Field(default=28.0)
    copy_dd_total_pct: float = Field(default=50.0)
    copy_loop_interval_seconds: int = Field(default=5)
    copy_wallet_poll_seconds: int = Field(default=10)
    copy_cluster_window_minutes: int = Field(default=15)


@lru_cache(maxsize=1)
def get_copy_settings() -> CopySettings:
    return CopySettings()


# ---------------------------------------------------------------------------
# Signal-type metadata used by sizing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SignalSpec:
    name: str
    stop_pct: float
    take_profit_pct: float
    timeout_hours: int


SIGNAL_SPECS: dict[str, SignalSpec] = {
    "cluster_buy": SignalSpec(
        name="cluster_buy",
        stop_pct=EXIT_STOP_PCT,
        take_profit_pct=EXIT_TAKE_PROFIT_PCT,
        timeout_hours=EXIT_TIMEOUT_HOURS,
    ),
}


def cluster_size_to_pct(cluster_size: int) -> float:
    """Map cluster size → position size %."""
    if cluster_size < CLUSTER_MIN_WALLETS:
        return 0.0
    if cluster_size in CLUSTER_SIZE_TO_PCT:
        return CLUSTER_SIZE_TO_PCT[cluster_size]
    return CLUSTER_SIZE_LARGE_PCT
