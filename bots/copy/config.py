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

import os
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
# Per-wallet notional floor. Locked Item #7 spec was $5k → $1k (2026-05-24, only
# 2.7% of webhook buys clear $5k; median single-wallet buy = $264). Now ENV-TUNABLE
# (2026-06-26) via COPY_CLUSTER_MIN_NOTIONAL_PER_WALLET_USD so the funnel can be
# widened without a redeploy. Code default stays $1k; the server .env runs $500 to
# get more cluster N now that the entry gates (liquidity floor + persistence delay)
# cover the fast-dump downside — see the gate analysis 2026-06-26.
# ⚠ LOCKED v0 threshold: lowering it EXPANDS the behavior space, so per the
# kill-criteria lock rule (top of file) it RESETS the cluster kill-criteria window.
CLUSTER_MIN_NOTIONAL_PER_WALLET_USD = float(
    os.environ.get("COPY_CLUSTER_MIN_NOTIONAL_PER_WALLET_USD", "1000")
)
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
    # Active-tier size target. Intentionally >75 (Roy, 2026-06): if proper
    # PnL-based promotion works and there are >75 wallets contributing to
    # profits, a larger active list captures more of the edge. Drives the
    # daily cron's promote/demote/swap math — MUST match the real intent or
    # swap_in mass-churns (the 2026-06-21 bug: code said 75, active was 124,
    # so swap_in fired 24-92x/day). Birdeye CU scales with active size; at
    # current usage (2.5% of 2.5M) there's headroom, but watch this as it grows.
    copy_active_list_target: int = Field(default=300)
    # Vetted-only promotion (2026-06-22). Only browser_opus* (curated/vetted)
    # wallets get promoted to active. Set false to revert to the old
    # activity-ranked promotion of any watch wallet. See wallet_pool_manager.
    copy_promote_vetted_only: bool = Field(default=True)
    # Rug detection floor for paper sells. LOWERED 2026-06-25 from $1000 → $50.
    # At paper-close we check Birdeye liquidity; only a near-zero pool (< this
    # floor) is a genuine rug → book ~total loss ('rug_no_liquidity'). The old
    # $1000 floor was WAY too high for fresh pump.fun mints (which legitimately
    # trade with $25-300 liquidity) and booked thin-but-LIVE tokens at a
    # fictitious -100% (e.g. a token down 41% with $294 liquidity booked as
    # -$400). Thin-but-live tokens now book at their real price with
    # liquidity-aware exit slippage (see _build_paper_exit / _liquidity_aware_exit_price).
    copy_rug_liquidity_floor_usd: float = Field(default=50.0)
    # Floor for modeled paper slippage (2026-06-21). The dex_quoter fix
    # (0f7e73c) switched entry slippage from a flat 100bps estimate to
    # Jupiter's priceImpactPct (~3bps), which is unrealistically optimistic
    # for fresh-mint memecoins and biased paper PnL high (avg dropped to
    # 3.4bps). Floor the modeled slippage so paper assumes realistic
    # memecoin friction. Set to 0 to disable the floor.
    copy_min_paper_slippage_bps: float = Field(default=150.0)
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

    # Cluster entry guards (2026-06-26). Same fast-death pattern as conviction:
    # cluster's <5min trades were -$1,692 / 0 wins while 5-15min holds carried the
    # edge (+$779). Two levers, both config-tunable, both fail-open:
    #  - liquidity floor: skip tokens too thin to round-trip our position (catches
    #    the $1-liq pump.fun rugs). Only a KNOWN-thin reading blocks; a miss doesn't.
    #  - persistence delay: park a cluster trigger this long, then enter only if the
    #    price hasn't cratered (dump already underway). Cluster's edge is the fast
    #    co-buy pump, so the delay is SHORTER than conviction's 75s — the big fast
    #    rugs die in 5-50s, so ~40s catches them at lower edge-cost. 0 disables.
    copy_cluster_min_entry_liquidity_usd: float = Field(default=5000.0)
    copy_cluster_entry_delay_seconds: int = Field(default=40)
    copy_cluster_confirm_max_adverse_pct: float = Field(default=25.0)

    # ------------------------------------------------------------------
    # Conviction mode (2026-06-24) — single-wallet trigger strategy
    # ------------------------------------------------------------------
    # Parallel COPY strategy that fires a paper buy when ONE elite "conviction"
    # wallet buys (no cluster needed). Own $10k paper bankroll + isolated
    # metrics so it can be measured/killed/promoted independently of the
    # cluster strategy, WITHOUT disturbing the cluster's pre-registered
    # evaluation. Roster lives in the DB (wallet_pool.conviction = true), not
    # here — edit via scripts/set_conviction_wallets.py. Ships dark (disabled);
    # flip on after reviewing the signal-frequency preview.
    copy_conviction_enabled: bool = Field(default=False)
    # Separate paper bankroll for conviction. Read by dd_monitor +
    # kill_criteria_monitor via env (COPY_CONVICTION_PAPER_CAPITAL_USD), same
    # as the cluster bankroll. Sizing/allocation below are computed against it.
    copy_conviction_paper_capital_usd: float = Field(default=10_000.0)
    # DEPRECATED 2026-06-25 — the single-buy floor was replaced by the
    # cumulative-accumulation trigger below (Birdeye analysis: these wallets
    # build winners from many sub-$1k clips, so no single-buy floor works).
    # Field kept only so a stray env var won't error; nothing reads it.
    copy_conviction_min_notional_usd: float = Field(default=1_000.0)
    # Cumulative-accumulation trigger (2026-06-25). The conviction detector sums
    # a roster wallet's buys per token over a rolling window and fires when the
    # committed total crosses the threshold (a single large buy crosses
    # instantly). dust_floor drops routing/fee junk; sell_holdoff suppresses the
    # trigger when the wallet is ALSO selling the token in the window (churn /
    # distribution, not clean accumulation). All env-overridable so the threshold
    # can be re-tuned without a redeploy. STARTING values — monitor + adjust the
    # threshold after ~20-30 conviction trades to find the sweet spot.
    copy_conviction_dust_floor_usd: float = Field(default=10.0)
    copy_conviction_accumulation_threshold_usd: float = Field(default=200.0)
    copy_conviction_accumulation_window_minutes: int = Field(default=60)
    # Window sells above this USD → hold off the buy. 0 = ANY non-dust sell of the
    # token in the window holds us off. Raise if it over-suppresses HF
    # accumulators that take tiny profits mid-build.
    copy_conviction_sell_holdoff_usd: float = Field(default=0.0)
    # Per-trade size = this % of the conviction bankroll. 4% mirrors the
    # cluster 3-wallet base (Roy 2026-06-24: keep at 4% — paper money).
    copy_conviction_sizing_pct: float = Field(default=4.0)
    # Allocation cap — total open conviction notional may not exceed this % of
    # the conviction bankroll. Mirrors the cluster 50% cap.
    copy_conviction_alloc_cap_pct: float = Field(default=50.0)
    # Follow-the-trigger-wallet-out exit: when TRUE, a conviction position is
    # closed as soon as the specific wallet that triggered it SELLS that token
    # (we already ingest sell events). The standard exit stack
    # (stop/TP/timeout/partials/trailing/sell-cluster) still applies on top.
    copy_conviction_follow_wallet_exit: bool = Field(default=True)
    # Entry liquidity guard (2026-06-25). Don't open a conviction position in a
    # token too thin to exit our ~$400 size without catastrophic slippage. Skip
    # the entry if the token's current Birdeye liquidity is below this USD floor.
    # Added after CyaE1Vx (a fresh-mint sniper) led conviction into ultra-thin
    # pump.fun mints ($26-294 liquidity) that cratered. Fail-open: a failed
    # liquidity fetch does NOT block (only a known-thin reading does). Tune by
    # observing whether it filters the rug-prone part of a wallet's signal.
    copy_conviction_min_entry_liquidity_usd: float = Field(default=5000.0)

    # Entry persistence gate (2026-06-26). After a conviction trigger fires, WAIT
    # this many seconds and re-confirm before actually entering. The fast-rug
    # losses all died in 19-47s (-$554 of conviction's losses), while real winners
    # held 16-35min — so waiting lets COPY see the token die before committing, at
    # near-zero cost to the winners. 0 = no delay (enter immediately; old behavior).
    copy_conviction_entry_delay_seconds: int = Field(default=75)
    # At the re-check, ABORT the entry if the token price has fallen more than this
    # percent below the trigger-time price (the dump already started — don't catch
    # the falling knife). 0 disables the price check. The whale-flip check (abort if
    # the trigger wallet net-sold the token during the wait) reuses
    # copy_conviction_sell_holdoff_usd as its tolerance.
    copy_conviction_confirm_max_adverse_pct: float = Field(default=25.0)

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

    # Comma-separated base58 token-creator wallets to block. Seeded with a
    # serial rug deployer identified in the 2026-06-10 audit: it minted
    # both "Doge Trillionaire" and "pack of cigarettes", BOTH net losses
    # for us (dumped too fast to ride). A cluster whose token creator is on
    # this list is SKIPPED at entry — a deliberate exception to the
    # ride-the-pump default (rugs are normally COPY's profit center),
    # justified only for operators whose tokens have a track record of
    # dumping before we can profit. Add creators here as the post-mortem
    # data identifies more net-loss serial deployers. NOTE: base58 is
    # case-sensitive — do not lowercase.
    copy_blocked_creators: str = Field(
        default="ERbjHyBxd1MYWTk8TvHJA84LfwAkAeQcxoZkdEusicaY"
    )

    def get_blocked_creators(self) -> frozenset[str]:
        """Parse `copy_blocked_creators` into a set of base58 addresses.
        Empty/malformed → empty set (fail-open: never block on a bad env)."""
        raw = (self.copy_blocked_creators or "").strip()
        if not raw:
            return frozenset()
        return frozenset(x.strip() for x in raw.split(",") if x.strip())

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
    # Conviction (single-wallet trigger) reuses the same exit stack as the
    # cluster strategy — only entry differs.
    "conviction_buy": SignalSpec(
        name="conviction_buy",
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
