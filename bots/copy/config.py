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

# Sell-cluster — exit-side detector (brainstorm 2026-05-30: "sell-cluster
# as LONG-SIDE STOPS first"). Lower wallet threshold than the buy side
# (2 vs 3) because we want exits to fire BEFORE the slide accelerates,
# and the false-positive cost is bounded (worst case is exiting too early
# on a paper-hands wallet, not opening a bad position). Per-wallet
# notional floor matches the buy side ($1k) — anything smaller is noise
# that would otherwise produce constant exit churn.
SELL_CLUSTER_MIN_WALLETS = 2
SELL_CLUSTER_MIN_NOTIONAL_PER_WALLET_USD = 1_000.0
SELL_CLUSTER_WINDOW_MINUTES = 15

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
EXIT_TAKE_PROFIT_PCT = 30.0                # static TP — only fires before trailing activates
EXIT_TIMEOUT_HOURS = 12                    # cluster signals decay fast

# Trailing stop + partial exits — captures memecoin upside per brainstorm
# 2026-05-30 (Trader's R1) plus 2026-06-08 ladder refinement.
#
# Logic (applied per check cycle, in this order):
#   1. Update peak_pct_since_entry (monotonic, persisted to Trade.sim_metadata)
#   2. If current_pct <= -EXIT_STOP_PCT → static stop (downside protection)
#      [full close, no partials — we never sell pieces of a loser]
#   3. For each tier in PARTIAL_EXIT_TIERS whose threshold is met by the
#      current peak_pct AND hasn't already fired: sell that tier's fraction
#      of the ORIGINAL position. Multiple tiers can fire in one cycle if
#      peak gap-up'd past several thresholds.
#   4. After partials, if peak_pct >= activation: check trailing stop on
#      the REMAINING position. Trailing is MULTIPLICATIVE — 25% drop in
#      price from peak, NOT 25 percentage points off the gain. This is
#      critical at high peaks: 25 pct-points off 4900% is ~0.5% of
#      price (would fire on noise); 25% multiplicative is a real pullback.
#   5. Static TP fallback (only reachable if peak somehow skipped activation)
#   6. Timeout — handled by caller, not the pure function
#
# Activation gate (20%) prevents trailing from firing on early-stage noise;
# the static -8% stop is the safety net during that period.
#
# PARTIAL_EXIT_TIERS in peak_pct (percentage gain from entry):
#   - 200% peak = 3x  → sell 25% (recoups ~75% of cost basis)
#   - 900% peak = 10x → sell 25% (recoups full cost basis after this tier)
#   - 4900% peak = 50x → sell 25%
#   - 99900% peak = 1000x → sell 25% (final close)
# After all four tiers: 0% remaining. The trade row closes with
# exit_reason='tier_complete'.
EXIT_TRAILING_ACTIVATION_PCT = 20.0
EXIT_TRAILING_STOP_PCT = 25.0          # interpreted MULTIPLICATIVELY: 25% drop in price from peak

# Deprecated 2026-06-08 with the tiered partial-exit ladder. Tier 4 of
# PARTIAL_EXIT_TIERS at 99900% (1000x) is the effective ceiling now;
# this constant is kept ONLY for backwards-compat with any external
# code/tests that still import it. evaluate_exit_actions ignores it.
EXIT_TRAILING_HARD_CAP_PCT = 99900.0

# Hard-coded fallback in case env parsing produces an empty/invalid ladder.
# Live config is `CopySettings.get_partial_exit_tiers()` which parses
# COPY_PARTIAL_EXIT_TIERS env var. Default below matches Roy's 2026-06-08
# choice: 4x / 10x / 50x / 1000x.
PARTIAL_EXIT_TIERS: tuple[tuple[float, float], ...] = (
    (300.0, 0.25),     # 4x   (1 + 300/100)
    (900.0, 0.25),     # 10x
    (4900.0, 0.25),    # 50x
    (99900.0, 0.25),   # 1000x
)


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
    # Migrated 2026-06-08 from quote-api.jup.ag/v6/quote → lite-api endpoint.
    # See note in bots/copy/venue/jupiter_swap.py JUPITER_QUOTE_URL.
    jupiter_quote_url: str = Field(default="https://lite-api.jup.ag/swap/v1/quote")
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
    # Sell-cluster detector enabled flag. Default TRUE because it's a
    # defensive exit signal — even with COPY_LIVE_ENABLED=false (no
    # signing path active), the sell detector should observe and write
    # shadow_signals rows so we collect data on how often it would have
    # fired. The actual trade-close action only fires when a real
    # shadow/live position exists in the same token.
    copy_sell_cluster_enabled: bool = Field(default=True)

    # Persistent dedup window for cluster_detections (2026-05-30). Default
    # 24h aligns with the Statistician's data-independence requirement —
    # token 7m96tz fired 6 times across 16 days in shadow_log under the
    # in-memory 15-min suppression. 24h dedup collapses those to 1 obs/day.
    # Configurable so we can sweep (24/12/4/1) via the Grafana comparison
    # panel without code changes. NOT in the LOCKED v0 thresholds list —
    # this is data-hygiene infra, not signal logic, so changing it does
    # NOT reset the kill-criteria window per the operational-fix carve-out.
    copy_cluster_dedup_hours: int = Field(default=24)

    # ------------------------------------------------------------------
    # Live + shadow execution (2026-06-06 — executor build)
    # ------------------------------------------------------------------
    # Master gate. Default FALSE — full skeleton in code but no signing
    # path can fire until this is flipped. When false: paper-only,
    # identical to pre-executor behavior. When true: paper + sampled
    # shadow (and live, if the per-mode gate is also set).
    copy_live_enabled: bool = Field(default=False)
    # Bot-specific Solana keypair, base58-encoded 64-byte secret. NEVER
    # commit this to the repo; set only in .env on Hetzner. If empty,
    # is_wallet_available() returns False and all executor paths short
    # out before reaching the signing code.
    copy_solana_private_key: str = Field(default="")
    # Shadow sampling rate. Mirrors STRUCTURE_SHADOW_PCT semantics — a
    # roll < this % places a shadow trade pair to the paper trade.
    copy_shadow_pct: float = Field(default=10.0)
    # Hard cap on total open shadow notional. STRUCTURE uses $40 of $50
    # HL equity; COPY's target shadow bankroll is similar (~$50-100
    # USDC). Cap at $40 to leave $10 headroom for gas/rent.
    copy_shadow_open_cap_usd: float = Field(default=40.0)
    # Per-shadow notional band. Bottom is the Jupiter minimum sensible
    # swap (~$10 — below this and fees dominate). Top caps single-trade
    # blast radius.
    copy_shadow_notional_min_usd: float = Field(default=10.0)
    copy_shadow_notional_max_usd: float = Field(default=25.0)
    # Slippage tolerance for memecoin swaps. Memecoins routinely show
    # 5-15% impact on $20 swaps; HL's 2% would auto-reject most signals.
    # 1500 bps = 15%. Tune down once we see real fills.
    # KEPT for backwards compatibility with the original skeleton; new
    # code paths use the ladder below for adaptive escalation.
    copy_swap_slippage_bps: int = Field(default=1500)
    # Adaptive slippage ladder — bot tries each tier in order, escalating
    # when a tier fails with a tolerance-related error (quote_unavailable,
    # tx failed/dropped). Per brainstorm 2026-05-30 spec the default is
    # [200, 500, 1500, 3000] bps. Tight 200 fills cleanly on liquid
    # tokens; loose 3000 covers low-liquidity memecoins. Comma-separated
    # bps values; parse via get_slippage_ladder().
    copy_swap_slippage_ladder_bps: str = Field(default="200,500,1500,3000")
    # Compute-unit price (priority fee) in micro-lamports. 50_000 (=0.00005
    # SOL per CU at 1M CU cap → ~0.05 SOL = ~$10 at $200 SOL) is high
    # for a typical swap but ensures inclusion during congestion. Jupiter
    # picks the actual CU count; this is the rate.
    copy_swap_priority_fee_micro_lamports: int = Field(default=50_000)
    # Tx confirmation timeout. Solana finality is ~12s under normal load,
    # 30-45s during congestion. Drop to 30s once we observe stable times.
    copy_swap_confirm_timeout_sec: int = Field(default=45)
    # Live (full-exposure) gate. Even with copy_live_enabled=true, live
    # placement requires this. Two-key safety: skeleton ships with both
    # off; shadow gets enabled first; live only after shadow PnL +
    # calibration_ratio looks sane.
    copy_live_full_enabled: bool = Field(default=False)
    # Tier ladder for partial exits. Comma-separated `pct:fraction` pairs.
    # Default: 4x / 10x / 50x / 1000x with 25% each. Tweaking is just an
    # env var update + container recreate — no code change needed.
    # Notes:
    # - pct is peak_pct_since_entry threshold (e.g. 300 = 4x = +300%)
    # - fraction is 0.0-1.0 share of ORIGINAL position sold at that tier
    # - Sum of fractions doesn't have to be 1.0 — leaving a long-term
    #   rider (e.g. 4 tiers × 20% = 80% sold, 20% rides forever on
    #   trailing/sell-cluster) is a valid configuration.
    # - Must be monotonically increasing by pct. If not, the loader
    #   sorts defensively but you should fix the env value.
    copy_partial_exit_tiers: str = Field(
        default="300:0.25,900:0.25,4900:0.25,99900:0.25"
    )

    def get_slippage_ladder(self) -> tuple[int, ...]:
        """Parse comma-separated `copy_swap_slippage_ladder_bps` into a
        tuple of ints. Returns a single-element fallback (1500 bps)
        if parsing fails, so the bot never crashes on a malformed env.
        """
        raw = self.copy_swap_slippage_ladder_bps or ""
        try:
            parsed = tuple(
                int(x.strip()) for x in raw.split(",") if x.strip()
            )
        except ValueError:
            parsed = ()
        return parsed or (self.copy_swap_slippage_bps or 1500,)

    def get_partial_exit_tiers(self) -> tuple[tuple[float, float], ...]:
        """Parse `copy_partial_exit_tiers` env (format:
        'pct:frac,pct:frac,...') into a tuple of (peak_pct, fraction)
        pairs. Defensive against malformed input: returns the module-level
        PARTIAL_EXIT_TIERS constant on any parse failure or empty result.

        Sorts the tiers by pct ascending if the input isn't ordered —
        evaluate_exit_actions assumes monotonic order, so this prevents
        silent misbehavior from a user typo.
        """
        raw = (self.copy_partial_exit_tiers or "").strip()
        if not raw:
            return PARTIAL_EXIT_TIERS
        try:
            parsed: list[tuple[float, float]] = []
            for item in raw.split(","):
                item = item.strip()
                if not item:
                    continue
                pct_str, frac_str = item.split(":", 1)
                pct = float(pct_str.strip())
                frac = float(frac_str.strip())
                if pct < 0 or frac <= 0 or frac > 1.0:
                    raise ValueError(f"invalid tier ({pct}, {frac})")
                parsed.append((pct, frac))
            if not parsed:
                return PARTIAL_EXIT_TIERS
            parsed.sort(key=lambda x: x[0])
            return tuple(parsed)
        except (ValueError, IndexError):
            return PARTIAL_EXIT_TIERS


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
