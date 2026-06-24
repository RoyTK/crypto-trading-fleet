## Architecture & Codebase Map

_Last reviewed: 2026-06-24_

For an engineer who has never seen the repo. Deeper, frozen design rationale lives in
`design_state_2026-04-26.md` (a snapshot — treat as history, not current truth).

### High-level shape

A shared **framework** + two **bots**, all Python, all running as Docker containers on one
Hetzner host, backed by **PostgreSQL** (state) and **Redis** (pubsub/dedup). Code reaches
the server by `git push` (auto-pull deploy). Monitoring is **Grafana/Prometheus** +
multi-channel alerting (Discord/Telegram/Twilio).

```
                 ┌────────── framework/ (shared) ──────────┐
                 │ DB models, kill-criteria, alerts, audit, │
                 │ heartbeat/watchdog, reconciliation,      │
                 │ scoring, reporting                       │
                 └──────────────────────────────────────────┘
   bots/structure/  (Hyperliquid perps)     bots/copy/  (Solana memecoins)
            │                                       │
        Postgres  ◄───────── shared ───────────►  Redis
            │                                       │
   monitoring/ (webhook_receiver, alerting dispatcher, dashboards, prometheus)
```

### The bots

**STRUCTURE** (`bots/structure/`) — Hyperliquid perpetual futures. Signals in
`bots/structure/signals/`: `funding_fade`, `liquidation_cascade`, `whale_flip`,
`hl_oi_divergence`. Reads market data via the Hyperliquid Info API (`venue.py`). Paper +
~10% shadow. **Currently PAUSED** — it was losing paper money; the suspected fix needs a
~$500/mo real-time data-feed upgrade (e.g. the Coinglass liquidation tier feeding
`liquidation_cascade`), not justified in the paper phase. Code is intact; revisit later.

**COPY** (`bots/copy/`) — Solana memecoins via wallet-cluster copying. This is the active
focus. Key files:
- `main.py` — the loop: subscribes to wallet events, runs cluster detection, manages
  positions.
- `signals/cluster.py` — the buy signal (≥3 distinct active wallets buy the same token
  within a 15-min rolling window, ≥$1k each).
- `signals/sell_cluster.py` — the defensive exit (≥2 tracked wallets selling).
- `trailing_stop.py` — tiered partial-exit ladder + price-scale anomaly guard.
- `wallet_pool_manager.py` — **pure logic** for active/watch/pruned tier decisions.
- `venue/` — external integrations: `helius_webhooks.py`, `helius_solana.py`, `birdeye.py`,
  `dex_quoter.py`, `jupiter_swap.py`, `cielo.py`.
- `config.py` — locked thresholds (see *Changing Safely*). **Current state: cluster buys
  ENABLED; `copy_active_list_target = 300`; KEEP wallets go directly to `active`.**

### COPY data flow (webhook → trade → measurement)

1. A tracked wallet trades on Solana → **Helius** sends a webhook to
   `monitoring/webhook_receiver/main.py` (one endpoint for `active`, one for `watch`).
2. The receiver validates the auth header, logs a row to `wallet_events_log`
   (`source_webhook` = active|watch), and for **active** wallets publishes a buy/sell event
   to Redis (`copy:buys` / `copy:sells`).
3. `bots/copy/main.py` subscribes, feeds buys to `signals/cluster.py` (in-memory 15-min
   rolling window per token).
4. When ≥3 distinct active wallets cluster on a token, a signal fires → de-duped via the
   `cluster_detections` table → a paper trade is opened (`executor.py` → `trades` row),
   optionally a ~10% **shadow** swap via Jupiter.
5. Positions are managed every ~60s: price via Birdeye/Jupiter, tiered partial exits,
   stops, **rug check** (Birdeye liquidity < floor → book ~total loss), sell-cluster exit.
6. On close, **PnL is attributed** equally to the wallets in the cluster
   (`wallet_attributions`) — this feeds wallet promotion/demotion and the leaderboard.
7. **kill-criteria** (`framework/kill_criteria_monitor.py`) reads closed trades and scores
   the bot (N, win rate, net PnL, Sharpe) against its window.

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
- `scoring/` — promotion score; `reporting/` — daily report + digest.

### Monitoring

- `monitoring/webhook_receiver/` — the public HTTPS endpoint Helius calls.
- `monitoring/alerting/` — taxonomy (P0–P3), dispatcher, connectors.
- `monitoring/dashboards/*.json` — Grafana (fleet-overview, structure-detail, copy-detail,
  cluster diagnostics); auto-reloaded, no restart needed.
- `monitoring/prometheus/` — scrape config.

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
