"""COPY bot configuration.

Threshold and sizing constants are locked from Item #7 of the design agenda.
Runtime knobs come from .env via pydantic-settings.

Do NOT change locked thresholds during the paper window — anti-gaming armor
requires signal logic to be frozen. Tuning happens during shakedown debug
(before paper clock starts) and during quarterly ops review.

======================================================================
KILL CRITERIA WINDOW LOCK — 2026-05-25 through 2026-07-24 (primary)
======================================================================
The constants in the "Locked v0 thresholds" section below are part of the
kill-criteria reset rule (see memory/project_decision_log.md, entry dated
2026-05-25). Any change to a constant in that section RESETS the kill-criteria
window day-counter UNLESS:
  1. You write a justification in audit_log BEFORE making the change
  2. The justification names which observation forced the change
  3. The change SHRINKS the bot's behavior space (tighter threshold,
     more conservative sizing) — not expands it
  4. No data-driven correction is allowed more than once per parameter
     per window

Wallet pool churn through the EXISTING active/watch tier rules does NOT
reset (that's the system working as designed). Logging / dashboards /
monitoring / bug fixes do NOT reset.
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
# Per-wallet notional floor. Locked Item #7 spec was $5k. Re-lowered to $1k
# 2026-05-24 after live data showed only 2.7% of webhook buys clear $5k
# (median single-wallet buy = $264; max in 30s sample = $9k). At $5k, only
# 1 cluster signal fired in 42 hours. At $1k (per Build A debug period),
# the pipeline produces enough cluster events for paper trades to accumulate
# meaningful data. Evaluate raising back to $2-3k once attribution data shows
# whether the $1k-$5k tier is signal-positive or noise.
CLUSTER_MIN_NOTIONAL_PER_WALLET_USD = 1_000.0
CLUSTER_TOKEN_MAX_AGE_HOURS = 24           # token age <24h OR vol jumped >5×
CLUSTER_VOL_JUMP_THRESHOLD = 5.0           # last-hour vol vs prior 24h avg
# Drop wallets with extreme trade counts from pool — they're MM bots, not
# alpha. 50k+ trades over 1 year ≈ 137 trades/day ≈ obvious bot.
WALLET_MAX_TRADE_COUNT_FOR_POOL = 50_000

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

# Wallet curation criteria — tuned 2026-05-04 for Solana memecoin reality.
# Original Item #7 thresholds (30min-7d hold, 60d wallet age) were calibrated
# for HL futures swing traders. Solana memecoin alpha rotates in MINUTES.
# Tuning is allowed pre-paper-clock; locked once paper trading begins.
#
# Locked rationale:
# - PnL ≥ $50k: meaningful absolute profit, filters out grinders
# - WR ≥ 0.55: better-than-coinflip across many trades
# - swap_count ≥ 20: enough trades to be statistically meaningful
# - hold ≥ 1 min: only excludes true HFT/MM bots (sub-1-min holds)
# - hold ≤ 7d: excludes pure long-term holders (we want active rotators)
WALLET_MIN_PNL_USD = 50_000.0
# Cielo's winrate field is on a 0-100 percentage scale, NOT 0-1 ratio.
# Verified empirically 2026-05-04 — wallets returned wr values like 83.38, 5.26.
# So 55.0 = "≥55%" not "≥5500%".
WALLET_MIN_WIN_RATE = 55.0
WALLET_MIN_HOLD_MINUTES = 1
WALLET_MAX_HOLD_DAYS = 7
WALLET_MIN_TRADES_90D = 20
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

    # Birdeye (wallet discovery for curation)
    birdeye_api_key: str = Field(default="")

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
    # Position management (exit check) cadence. Paper trades don't need
    # 5-sec resolution on stop/TP triggers; 60s avoids hammering the
    # price oracle (Birdeye free tier is 1 RPS — 6+ open positions × 5s
    # iterate burns through the budget instantly).
    copy_position_check_seconds: int = Field(default=60)
    # Solana polling cadence (Helius — cheap, 10M credits/mo)
    copy_wallet_poll_seconds: int = Field(default=10)
    # EVM polling cadence (Cielo — only 50k credits/mo on Pro; with 90 EVM wallets
    # at 60s, that's ~130k req/mo — still over. Default to 120s = ~65k req/mo.
    # Tune via env var if Cielo's cost-per-call turns out lower than worst-case.)
    copy_evm_wallet_poll_seconds: int = Field(default=120)
    copy_cluster_window_minutes: int = Field(default=15)
    # Pause flag for cluster_buy trading (default true for backwards compat).
    # Set false to stop DEX trades while keeping wallet pool, Helius webhooks,
    # cluster detector, and shadow log all running. Signed 2026-05-28 per
    # adversarial team meeting on COPY's strategic future.
    copy_cluster_buy_enabled: bool = Field(default=True)
    # Shadow log poll cadence — how often to fetch updated prices for
    # pending shadow_log rows + compute MFE/MAE. 5min matches Birdeye
    # historical candle resolution.
    copy_shadow_log_poll_seconds: int = Field(default=300)
    # Persistent dedup window for cluster_detections (2026-05-30). Default
    # 24h aligns with the Statistician's data-independence requirement —
    # token 7m96tz fired 6 times across 16 days in shadow_log under the
    # in-memory 15-min suppression. 24h dedup collapses those to 1 obs/day.
    # Configurable so we can sweep (24/12/4/1) via the Grafana comparison
    # panel without code changes. NOT in the LOCKED v0 thresholds list —
    # this is data-hygiene infra, not signal logic, so changing it does
    # NOT reset the kill-criteria window per the operational-fix carve-out.
    copy_cluster_dedup_hours: int = Field(default=24)


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
