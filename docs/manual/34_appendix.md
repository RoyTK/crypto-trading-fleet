## Appendix

_Last reviewed: 2026-06-24_

### Crontab inventory (server, UTC)

Verify the live set with `crontab -l`. Expected:
- `* * * * *` — `scripts/hetzner_autopull.sh` (auto-deploy)
- `0 7 * * *` — `scripts.wallet_pool_daily_cron` (pool reconcile + Helius sync)
- `30 7 * * *` — `scripts.apply_vetting_results` (ingest vetting)
- `15 13 * * *` — `scripts.credit_pool_snapshot` (credit/pool curve)
- `~13:00` — `scripts.daily_digest` (COPY 24h summary — verify installed)
- every 5h — STRUCTURE whale_pool_growth
- weekly — STRUCTURE whale_graduation_scan
- `0 14 1 2,5,8,11 *` — `scripts.quarterly_whale_refresh`
- `0 6/14/22 * * *` — 3× `scripts.wallet_pool_discovery` — **PAUSED** (commented)

### Env-var reference (names + purpose only — values live in `.env`)

| Var | Purpose |
|---|---|
| `HELIUS_API_KEY`, `HELIUS_RPC_URL`, `HELIUS_WEBHOOK_AUTH_SECRET` | Solana webhooks/RPC + receiver auth |
| `CIELO_API_KEY` | wallet PnL stats |
| `BIRDEYE_API_KEY` | token data |
| `COPY_LIVE_ENABLED`, `COPY_LIVE_FULL_ENABLED` | real-money gates (OFF) |
| `COPY_SOLANA_PRIVATE_KEY` | COPY trading wallet secret (empty until live) |
| `COPY_CLUSTER_BUY_ENABLED` | COPY buying on/off (currently ON) |
| `COPY_DISCORD_WEBHOOK` | discovery health pings |
| `HYPERLIQUID_AGENT_PRIVATE_KEY`, `HYPERLIQUID_*` | STRUCTURE venue (trade-only key) |
| `COINGLASS_API_KEY`, `STRUCTURE_LIQ_CASCADE_ENABLED` | liquidation feed (disabled) |
| `DISCORD_*`, `TELEGRAM_*` | alerting channels (active) |
| `TWILIO_*` | SMS — **not configured** (no SMS in paper phase) |
| `SMTP_*`, `SMTP_TO` | email digest/report; `SMTP_TO` = `trading@generalaisystems.com` |
| `CLOUDFLARE_TUNNEL_TOKEN` | remote access |
| drawdown/halt knobs (`*_DD_*_PCT`, `CONSECUTIVE_LOSS_*`, `FLEET_*`) | risk limits |

See `.env.example` for the full list.

### Key files

| Path | What |
|---|---|
| `docker-compose.yml` | all services |
| `scripts/hetzner_autopull.sh` | auto-deploy |
| `bots/copy/config.py`, `bots/structure/config.py` | locked thresholds |
| `bots/copy/wallet_pool_manager.py` | tier-decision logic |
| `scripts/apply_vetting_results.py` | vetting → pool |
| `scripts/credit_pool_snapshot.py` | credit/pool snapshot |
| `bots/copy/discovery_automation/` | wallet discovery prompt + (attended) wrapper |
| `framework/models.py` | DB tables |
| `framework/kill_criteria_monitor.py` | scorecard + window dates |

### DB tables (quick map)

`bot_state` (state + kill-criteria JSON), `trades`, `signals`, `wallet_pool`,
`wallet_events_log`, `wallet_attributions`, `cluster_detections`, `shadow_signals`,
`copy_signal_shadow_log`, `structure_whale_pool`, `halts`, `scores`, `audit_log`,
`heartbeats`.

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
