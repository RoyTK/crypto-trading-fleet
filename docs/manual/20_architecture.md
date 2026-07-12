## Architecture & Codebase Map

_Last reviewed: 2026-07-02_

For an engineer who has never seen the repo. Deeper, frozen design rationale lives in
`design_state_2026-04-26.md` (a snapshot — treat as history, not current truth).

### High-level shape

A shared **framework** + one **bot** (`bots/copy/`, running **five strategies**), all
Python, all running as Docker containers on one Hetzner host, backed by **PostgreSQL**
(state) and **Redis** (pubsub/dedup). Code reaches the server by `git push` (auto-pull
deploy). Monitoring is **Grafana/Prometheus** + multi-channel alerting (Discord/Telegram).
`bots/structure/` remains in the tree but is **decommissioned** (not built into
docker-compose, not running).

```
                 ┌────────── framework/ (shared) ──────────┐
                 │ DB models, kill-criteria, alerts, audit, │
                 │ heartbeat/watchdog, reconciliation,      │
                 │ scoring, reporting                       │
                 └──────────────────────────────────────────┘
        bots/copy/  (Solana memecoins — 5 strategies: cluster · conviction ·
                     teamfollow · cohortfire · promobuy)
            │                                       │
        Postgres  ◄───────── shared ───────────►  Redis
            │                                       │
   monitoring/ (webhook_receiver, alerting dispatcher, dashboards, prometheus)

   (bots/structure/ retained in-tree but DECOMMISSIONED 2026-06-25)
```

### The bot & its strategies

**COPY** (`bots/copy/`) — Solana memecoins. This is the whole fleet. It runs under a single
`bot_id='copy'`; the **five strategies** are discriminated by `sim_metadata->>'strategy'` and
each keeps isolated metrics, its own paper bankroll, and its own halt id. The first three copy
wallets; the last two (added 2026-07-12) act on upstream signals — a coordinated-sniper roster
and paid promotion:

1. **cluster** — ≥3 distinct active wallets co-buy the same token within a 15-min window;
   40s entry delay; **$50k** entry-liquidity floor (raised from $5k on 2026-07-01); halt id
   `copy`. Exit stack: −8% hard stop (widens to −30% when EMA liquidity ≥1.5× entry), partial
   ladder 4x/10x/50x/1000x (25% each), 45% multiplicative trailing stop after +20%,
   sell-cluster + price-scale guard, 12h timeout.
2. **conviction** — a **single** roster wallet *deliberately accumulates* one token (≥3 buys
   over ≥5 min — the accumulation gate shipped 2026-07-01, not single-buy snipes). Own $10k
   paper bankroll; halt id `copy_conviction`; 25% hard stop. Roster ≈ 23 DB-forward-validated
   accumulators, **reloaded only at `bot_copy` startup** (`docker compose restart bot_copy`
   after a roster change).
3. **teamfollow** *(experiment, shipped 2026-07-01)* — ≥2 members of the same known "team"
   co-buy within a window, from a **128-team / 336-wallet** roster
   (`bots/copy/teamfollow_roster.json`; team 96 pruned 2026-07-04 as a fresh-mint sniper pair).
   Own $25k paper bankroll; **two entry gates: $50k liquidity floor + a 6-hour minimum token-age
   gate** (`copy_teamfollow_min_token_age_hours`, added 2026-07-04 — teamfollow's losses were
   almost entirely fresh-mint rugs, so entries into tokens <6h old are blocked); reuses the
   cluster exit stack; halt id `copy_teamfollow`; isolated Helius webhook + Redis channel
   `copy:teamfollow_buys`. **Reset 2026-07-04** (prior trades retagged `teamfollow_pre_reset`) to
   measure the gated version on a clean window — a forward test of the "many small losses, rare
   moonshots pay" thesis. Low trade volume (~few/day) is genuine signal scarcity after the gates,
   not a bug (verified: the teams co-buy almost only fresh mints / thin tokens, both filtered).
4. **cohortfire** *(experiment, 2026-07-12)* — like teamfollow, but the roster is the
   **RED-COHORT** academic catalogue of coordinated launch-sniper rings (arXiv 2602.13480,
   CC-BY; `bots/copy/cohortfire_roster.json`, 81 wallets / 22 cohorts, all live in the watch
   tier). Fires when ≥2 members of the same cohort co-buy. Own $25k bankroll; halt id
   `copy_cohortfire`; Redis channel `copy:cohort_buys`; same 6h/$50k gates + cluster exit stack.
   The webhook receiver republishes those wallets' watch-tier buys to the channel (watch is
   otherwise observe-only). Thesis: teamfollow's biggest wins came from sniper rings *re-engaging
   an established token* — this tests that at scale on a validated roster.
5. **promobuy** *(experiment, 2026-07-12)* — buys on **paid Dexscreener promotion** (token
   profiles / boosts / community-takeovers). Not a wallet strategy: the `promo_shadow_collector`
   cron publishes each newly-promoted token to `copy:promo_signals`; the bot evaluates each one
   directly. Own $25k bankroll; halt id `copy_promobuy`; gates = $20k liquidity + a 0–72h token-age
   window + creator blocklist, computed from one Dexscreener `/tokens/v1` call (which also yields
   the practitioner filter features — liq/mcap ratio, buy/sell ratio, volume acceleration). Thesis
   (measured): ~65% of runners had paid promo, which precedes the run by ~13h.

Every strategy logs a MELT-style feature snapshot at entry (live holder concentration, cohort
overlap, wash/order-flow) into `signal_features` via `bots/copy/signal_features.py`, building a
labeled dataset to later train a multi-factor quality model. All experiment strategies (teamfollow,
cohortfire, promobuy) default **OFF** and are enabled per-`.env` flag.

Key COPY files:
- `main.py` — the loop: subscribes to the wallet/cohort/promo event streams, runs the five
  strategies' detectors, manages positions.
- `signals/cluster.py` — the cluster buy signal.
- `signals/teamfollow.py` — group-cobuy detector (reused by cohortfire via a `strategy_name` arg).
- `signals/sell_cluster.py` — the defensive exit (≥2 tracked wallets selling).
- `trailing_stop.py` — tiered partial-exit ladder + price-scale anomaly guard.
- `wallet_pool_manager.py` — **pure logic** for active/watch/pruned tier decisions.
- `teamfollow_roster.json` / `cohortfire_roster.json` — the group-cobuy experiment rosters.
- `signal_features.py` — MELT-style feature snapshot logged at every entry (`signal_features` table).
- `venue/` — external integrations: `helius_webhooks.py`, `helius_solana.py`, `birdeye.py`,
  `dex_quoter.py` (Birdeye + Dexscreener `/tokens/v1`), `jupiter_swap.py`, `cielo.py`.
- `config.py` — locked thresholds (see *Changing Safely*). **Current state: cluster buys
  ENABLED; `copy_active_list_target = 500` (raised from 300 on 2026-07-10 to grow
  the active pool into the ~3M/mo of unused prepaid Helius plan credits); KEEP
  wallets go directly to `active`.**

**STRUCTURE** (`bots/structure/`) — *decommissioned 2026-06-25.* Formerly Hyperliquid
perpetual futures (signals `funding_fade`, `liquidation_cascade`, `whale_flip`,
`hl_oi_divergence`). Removed from docker-compose + the kill/dd monitors + scoring crons; the
code is intact but **not running**. Revive = restore the compose block + re-add its cron
hooks. Was losing paper money; the suspected fix needs a ~$500/mo real-time data feed not
justified in the paper phase.

### COPY data flow (webhook → trade → measurement)

1. A tracked wallet trades on Solana → **Helius** sends a webhook to
   `monitoring/webhook_receiver/main.py`. There are **three subscription tiers**, each with its
   own webhook: `active`, `watch`, and `teamfollow`.
2. The receiver validates the auth header, logs a row to `wallet_events_log`
   (`source_webhook` = active|watch|teamfollow), and publishes buy/sell events to Redis —
   `copy:buys` / `copy:sells` for active wallets, `copy:teamfollow_buys` for the team-follow
   roster, and (for watch-tier wallets that are on the RED-COHORT roster) `copy:cohort_buys`.
   Separately, the `promo_shadow_collector` cron publishes paid-promo tokens to `copy:promo_signals`.
3. `bots/copy/main.py` subscribes to all those channels and feeds the five detectors: cluster
   (15-min rolling co-buy window), conviction (per-wallet accumulation ≥5 min), teamfollow +
   cohortfire (≥2 same-group co-buy), and promobuy (per promo signal).
4. When a strategy fires, the signal is de-duped (e.g. `cluster_detections`) → a paper trade
   is opened (`executor.py` → `trades` row, stamped with the `strategy` in `sim_metadata`),
   optionally a ~10% **shadow** swap via Jupiter. Each strategy honors its own
   entry-liquidity floor (cluster/teamfollow = $50k).
5. Positions are managed every ~60s: price via Birdeye/Jupiter, tiered partial exits,
   stops, **rug check** (Birdeye liquidity < floor → book ~total loss), sell-cluster exit.
   Each trade also stamps `sim_metadata.token_age_at_entry_hours` (+ `token_created_unix`)
   at entry, and records `peak_pct_since_entry` (max favorable excursion while held).
6. On close, **PnL is attributed** equally to the wallets in the cluster
   (`wallet_attributions`, a fair `pnl/cluster_size` share) — this feeds wallet
   demotion/pruning and the leaderboard.
7. **kill-criteria** (`framework/kill_criteria_monitor.py`) reads closed trades and scores
   each strategy (N, win rate, net PnL, Sharpe) against its window; the headline measure is
   **net PnL** (the old composite promotion_score is retired).

### The wallet pool lifecycle

- New wallets enter **already vetted** (via browser discovery) and go **straight to
  `active`** (KEEP), applied by `scripts/apply_vetting_results.py`.
- The daily job `scripts/wallet_pool_daily_cron.py` recomputes activity, **demotes** proven
  losers (by attributed PnL, not raw activity), **prunes** persistent losers, and **syncs**
  the active+watch address lists to Helius (`venue/helius_webhooks.py::sync_pool_tiers`).
  Both active and watch are webhook-subscribed (so both cost Helius credits).
- `wallet_pool_manager.decide_tier_changes()` is the pure decision function (well tested in
  `tests/test_wallet_pool_manager.py`). Promotion is **vetted-only**
  (`copy_promote_vetted_only`).

### The framework

- `framework/models.py` — all DB tables (see Appendix for the table list). Key ones:
  `trades`, `signals`, `wallet_pool`, `wallet_events_log`, `wallet_attributions`,
  `cluster_detections`, `bot_state` (holds `kill_criteria_status` JSON), `halts`, `scores`,
  `audit_log`, `heartbeats`.
- `kill_criteria_monitor.py` — window dates + scoring; **alerts only, no auto-halt** for
  strategy criteria.
- `dd_monitor.py` / `halt_state.py` — drawdown limits that **do** auto-halt; `/panic`.
- `alerts.py` (`emit_alert`) → Redis `alerts:emit` → `monitoring/alerting/dispatcher.py` →
  Discord/Telegram/Twilio connectors.
- `audit.py` (`write_audit`) → append-only `audit_log`.
- `heartbeat.py` / `watchdog.py` — every process pings every 30s; watchdog escalates
  staleness.
- `reconciliation.py` — periodic paper-vs-venue consistency check.
- `scoring/` — per-strategy scoring; `reporting/` — daily report + digest (net PnL per
  strategy; the old composite promotion_score is retired).

### Monitoring

- `monitoring/webhook_receiver/` — the public HTTPS endpoint Helius calls (active / watch /
  teamfollow tiers).
- `monitoring/alerting/` — taxonomy (P0–P3), dispatcher, connectors.
- `monitoring/dashboards/*.json` — Grafana, file-provisioned (~30s poll), auto-reloaded:
  **Fleet Overview** (strategy-aware per-strategy scorecard: state / open / trades-24h /
  net-total / net-24h / win-rate, plus open positions, last-50 closed with `peak_pct`,
  cumulative PnL by strategy, heartbeat, halts — all **five** strategies), **COPY Cluster**
  (`copy-detail.json`), **COPY Conviction** (`copy-conviction.json`), **COPY Team-Follow**
  (`copy-teamfollow.json`), **COPY Cohort-Fire** (`copy-cohortfire.json`), and **COPY Promo-Buy**
  (`copy-promobuy.json`). Closed-trade tables have clickable Birdeye token links. Each has a title
  panel at the top. (`structure-detail.json` remains in-tree but its bot is decommissioned.)
- `monitoring/prometheus/` — scrape config.

### Server-side research cron (scrape-runners)

`scripts/scrape_runners.py` — a daily host cron (04:10 UTC, in the `framework` container)
that discovers freshly-run tokens (GeckoTerminal trending/new/top), detects each token's
run-start (Birdeye `history_price`), fetches the pre-run trade window (Birdeye
`seek_by_time`), and records the wallets that accumulated **before** the run into a Postgres
table `prerun_accumulators` (with a scan-once ledger `prerun_scans`). It prints a recurrence
report of wallets appearing across ≥3 runners and **stages** candidates for later
forward-validation — it does **not** auto-promote anyone to a roster. Logs to
`~/logs/scrape_runners.log`.

Two more **shadow research crons** (never trade — they log a signal + track its forward outcome,
to decide later whether it's worth building on): `slow_cluster_detector.py` (05:30 UTC daily —
does slow multi-wallet accumulation predict a run?) and `promo_shadow_collector.py` (every 20 min
— logs every paid-promoted token + its liquidity/mcap/order-flow at signal time and its forward
multiple; also feeds the live **promobuy** strategy). These came out of the 2026-07 "information-
source" study (go upstream of the wallets to where memecoin picks originate). Full write-ups live
in `research/*.md`; the reusable manipulation/quality detectors are in
`scripts/manipulation_detectors.py`.

### Where to look for X

| Want to… | Look in |
|---|---|
| Change a COPY threshold | `bots/copy/config.py` (locked — read *Changing Safely* first) |
| Understand the buy signal | `bots/copy/signals/cluster.py` |
| Change wallet promote/demote logic | `bots/copy/wallet_pool_manager.py` + `tests/test_wallet_pool_manager.py` |
| Add/inspect a DB table | `framework/models.py` + `framework/alembic/versions/` |
| Change alert routing | `monitoring/alerting/` |
| Add a scheduled job | `scripts/` + the crontab (see *Operations (technical)*) |
| The wallet discovery prompt/automation | `bots/copy/discovery_automation/` |
