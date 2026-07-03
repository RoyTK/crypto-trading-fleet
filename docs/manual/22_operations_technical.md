## Operations (Technical)

_Last reviewed: 2026-07-02_

### Scheduled jobs

Two layers: **APScheduler** inside the `scoring`/`report_cron` containers (drawdown checks
every 5 min, kill-criteria every 60 min, scoring, daily report ~07:00 local), and the
**server OS crontab** for `docker compose exec` scripts. Verify the live crontab with
`crontab -l`. As of this writing it contains (Hetzner, UTC):

| Schedule | Job | Notes |
|---|---|---|
| `* * * * *` | `scripts/hetzner_autopull.sh` | auto-deploy |
| `0 7 * * *` | `scripts.wallet_pool_daily_cron` | recompute activity, demote/prune, **sync Helius** |
| `30 7 * * *` | `scripts.apply_vetting_results` | ingest vetting verdicts (after `docker compose cp` of the file) |
| `15 13 * * *` | `scripts.credit_pool_snapshot` | daily Helius credit/pool snapshot → audit_log + Discord |
| `~13:00` | `scripts.daily_digest` | COPY 24h summary, broken out **per strategy** (net PnL, not promotion_score) |
| `10 4 * * *` | `scripts/scrape_runners.py` | **NEW (2026-07-02)** fresh-runner → pre-run-accumulator discovery (framework container); stages candidates only; logs `~/logs/scrape_runners.log` |
| `0 6/14/22 * * *` | 3× `scripts.wallet_pool_discovery` | **PAUSED** (commented out — 0% keepers; replaced by browser discovery) |

The STRUCTURE crons (`whale_pool_growth`, `whale_graduation_scan`, `quarterly_whale_refresh`)
were **removed** when STRUCTURE was decommissioned (2026-06-25).

To re-pause/resume a cron line, edit non-interactively, e.g.:
`(crontab -l; echo '<line>') | crontab -` — never paste a cron line straight into the shell.

### Key scripts (`scripts/`)

| Script | Purpose |
|---|---|
| `hetzner_autopull.sh` | the per-minute deploy loop |
| `wallet_pool_daily_cron.py` | daily pool reconcile + Helius sync |
| `apply_vetting_results.py` | KEEP→active, REJECT→pruned, TOO_FAST→logged; idempotent; `--dry-run` |
| `credit_pool_snapshot.py` | credits-vs-wallets curve into audit_log (proxy = `wallet_events_log` count) |
| `helius_webhook_setup.py` | create/sync/`--list`/`--delete` Helius webhooks |
| `daily_digest.py` | COPY daily Discord summary (per-strategy net PnL) |
| `scrape_runners.py` | fresh-runner → pre-run-accumulator discovery → `prerun_accumulators` (staging only) |
| `correct_rug_trade.py` | fix a fictitious post-rug paper exit (targeted, audited) |
| `build_manual.py` | **this manual's builder** |

Run any with: `docker compose exec -T framework python -m scripts.<name> [args]`.

### Logs & state

```bash
docker compose ps                                   # what's running
docker compose logs -f --tail=50 bot_copy           # follow a service
docker compose exec -T postgres psql -U fleet -d fleet -c "<SQL>"   # query DB
tail -f ~/autopull.log                              # deploys
```

Useful SQL:
```sql
-- bot state + kill-criteria snapshot
SELECT bot_id, state, halted_until, halt_reason,
       kill_criteria_status->>'n' AS n,
       kill_criteria_status->>'wr' AS wr,
       kill_criteria_status->>'sharpe' AS sharpe
FROM bot_state;

-- wallet pool composition
SELECT tier, COUNT(*) FROM wallet_pool WHERE chain='solana' GROUP BY tier;

-- recent COPY trades
SELECT entry_at, asset, size_usd, exit_reason, pnl_usd
FROM trades WHERE bot_id='copy' ORDER BY entry_at DESC LIMIT 20;

-- is it alive?
SELECT process_name, last_ping_at FROM heartbeats ORDER BY last_ping_at DESC;
```

### Monitoring internals

- **Alert pipeline:** any code calls `framework.alerts.emit_alert(severity, title, body,…)`
  → Redis `alerts:emit` → `monitoring/alerting/dispatcher.py` → connectors
  (`discord_connector`, `telegram_connector`, `twilio_connector`). Severity → channel
  routing is in `monitoring/alerting/taxonomy.py`.
- **Heartbeats:** each process writes `heartbeats(process_name, last_ping_at)` every 30s;
  `framework/watchdog.py` escalates staleness (P1 → restart attempt → P0).
- **Grafana:** dashboards in `monitoring/dashboards/*.json`, auto-reloaded. The
  kill-criteria status is on the per-bot detail dashboards.
- **Health truth for COPY discovery:** "did a discovery pass produce rows" is the real
  signal (a dead browser exits cleanly with 0 rows) — that's why discovery reports a
  row-count to Discord.

### Wallet vetting — applying results manually

```bash
cd ~/crypto-fleet && git pull origin main && \
  docker compose cp bots/copy/vetted_watch_results.txt framework:/tmp/vw.txt && \
  docker compose exec -T framework python -m scripts.apply_vetting_results --file /tmp/vw.txt
```
(`docker compose cp` is required because the container runs **baked** code and can't see a
host file otherwise.)
