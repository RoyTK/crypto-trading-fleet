## Appendix

_Last reviewed: 2026-07-02_

### Crontab inventory (server, UTC)

Verify the live set with `crontab -l`. Expected:
- `* * * * *` — `scripts/hetzner_autopull.sh` (auto-deploy)
- `0 7 * * *` — `scripts.wallet_pool_daily_cron` (pool reconcile + Helius sync)
- `30 7 * * *` — `scripts.apply_vetting_results` (ingest vetting)
- `15 13 * * *` — `scripts.credit_pool_snapshot` (credit/pool curve)
- `~13:00` — `scripts.daily_digest` (COPY 24h summary, per-strategy — verify installed)
- `10 4 * * *` — `scripts/scrape_runners.py` (fresh-runner → pre-run-accumulator discovery; framework container)
- `0 6/14/22 * * *` — 3× `scripts.wallet_pool_discovery` — **PAUSED** (commented)

The STRUCTURE crons (`whale_pool_growth`, `whale_graduation_scan`,
`quarterly_whale_refresh`) were removed when STRUCTURE was decommissioned (2026-06-25).

### Env-var reference (names + purpose only — values live in `.env`)

| Var | Purpose |
|---|---|
| `HELIUS_API_KEY`, `HELIUS_RPC_URL`, `HELIUS_WEBHOOK_AUTH_SECRET` | Solana webhooks/RPC + receiver auth |
| `CIELO_API_KEY` | wallet PnL stats |
| `BIRDEYE_API_KEY` | token data |
| `COPY_LIVE_ENABLED`, `COPY_LIVE_FULL_ENABLED` | real-money gates (OFF) |
| `COPY_SOLANA_PRIVATE_KEY` | COPY trading wallet secret (empty until live) |
| `COPY_CLUSTER_BUY_ENABLED` | COPY buying on/off (currently ON) |
| `COPY_CLUSTER_MIN_ENTRY_LIQUIDITY_USD` | cluster entry-liquidity floor (=$50k as of 2026-07-01) |
| `COPY_CLUSTER_MIN_NOTIONAL_PER_WALLET_USD` | per-wallet co-buy size for the cluster trigger |
| `copy_conviction_stop_pct` | conviction hard stop (=25%) |
| `COPY_DISCORD_WEBHOOK` | discovery health pings |
| `HYPERLIQUID_AGENT_PRIVATE_KEY`, `HYPERLIQUID_*` | STRUCTURE venue — **decommissioned** (unused) |
| `COINGLASS_API_KEY`, `STRUCTURE_LIQ_CASCADE_ENABLED` | STRUCTURE liquidation feed — **decommissioned** (unused) |
| `DISCORD_*`, `TELEGRAM_*` | alerting channels (active) |
| `TWILIO_*` | SMS — **not configured** (no SMS in paper phase) |
| `SMTP_*`, `SMTP_TO` | email digest/report; `SMTP_TO` = `trading@generalaisystems.com` |
| `CLOUDFLARE_TUNNEL_TOKEN` | remote access |
| drawdown/halt knobs (`*_DD_*_PCT`, `CONSECUTIVE_LOSS_*`, `FLEET_*`) | risk limits |

See `.env.example` for the full list.

### Key files

| Path | What |
|---|---|
| `docker-compose.yml` | all services (no `bot_structure` — decommissioned) |
| `scripts/hetzner_autopull.sh` | auto-deploy |
| `bots/copy/config.py` | locked thresholds (`bots/structure/config.py` retained but unused) |
| `bots/copy/wallet_pool_manager.py` | tier-decision logic |
| `bots/copy/teamfollow_roster.json` | team-follow experiment roster (129 teams / 338 wallets) |
| `scripts/apply_vetting_results.py` | vetting → pool |
| `scripts/credit_pool_snapshot.py` | credit/pool snapshot |
| `scripts/scrape_runners.py` | fresh-runner → pre-run-accumulator discovery (staging) |
| `bots/copy/discovery_automation/` | wallet discovery prompt + (attended) wrapper |
| `framework/models.py` | DB tables |
| `framework/kill_criteria_monitor.py` | scorecard + window dates |

### DB tables (quick map)

`bot_state` (state + kill-criteria JSON), `trades` (strategy in `sim_metadata`), `signals`,
`wallet_pool`, `wallet_events_log`, `wallet_swaps_log`, `wallet_attributions`,
`cluster_detections`, `shadow_signals`, `copy_signal_shadow_log`, `halts`, `scores`,
`audit_log`, `heartbeats`. Plus `prerun_accumulators` + `prerun_scans` — created directly by
`scripts/scrape_runners.py` (raw SQL, not in `models.py`). `structure_whale_pool` remains but
is unused (STRUCTURE decommissioned).

### Deeper docs (read these for detail this manual summarizes)

- `OPERATIONS.md` — full ops runbook (phases, costs, deploy, backups, /panic).
- `OPS_CHEATSHEET.md` — dense quick reference (window rules, commands, services).
- `design_state_2026-04-26.md` — frozen original design snapshot (history, not current).
- `bots/copy/discovery_automation/README.md` — discovery automation specifics.
- `memory/` (Claude's notes): `project_decision_log.md`, `project_fleet_design_state.md`,
  `project_ops_gotchas.md`, `reference_helius_dashboard_access.md`,
  `reference_onedrive_vetting_files.md`, `feedback_wallet_curation_criteria.md`.

### Maintaining this manual

Edit the section files in `docs/manual/`, run `python scripts/build_manual.py`, commit the
sources + rebuilt `docs/MANUAL.md` (and `.html`). Docs changes don't restart anything. See
`docs/manual/README.md`.
