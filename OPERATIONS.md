# Operations Runbook

Living document. Updated as the fleet grows from Phase 0 to live deployment.

## Owner
roy@generalaisystems.ai

## Alert taxonomy
| Severity | Channels | Examples |
|----------|----------|----------|
| P0 | Twilio SMS + Discord ping + Telegram | Fleet halt, /panic invoked, position drift > threshold, total DD kill |
| P1 | Discord ping + Telegram | Per-bot DD halt, consecutive-loss limit hit, heartbeat silent >2 min |
| P2 | Discord (no ping) | Trade entries/exits, calibration drift, per-bot daily DD warning |
| P3 | Daily digest only | Routine activity, score updates |

## Daily check-in
- Time: 7:00 AM local (`DAILY_REPORT_TIMEZONE` in .env)
- Channels: Discord + email
- Contents: per-bot PnL, scores, trade count, calibration ratio, drawdown state, alerts in last 24h

## /panic command
- Available on Discord AND Telegram
- Authentication: `DISCORD_OWNER_USER_ID` / `TELEGRAM_OWNER_USER_ID`
- Sequence: cancel all open orders → close all positions market → halt all bots → confirmation summary

## Position reconciliation
- Cadence: every 5 minutes
- Drift threshold: 0.5% (bot-state vs venue-actual)
- Action on drift: halt bot, P1 alert

## Heartbeat
- Ping every 30 seconds
- 2 min silent → P1 alert
- 5 min silent → auto-restart attempt
- Restart failure → hard halt + P0 page

## Remote access policy
- **< $50,000 deployed capital**: Cloudflare Tunnel + auth (current default)
- **≥ $50,000 deployed capital**: VPN-only — Cloudflare Tunnel disabled
- Tracker: `DEPLOYED_CAPITAL_USD` in .env; threshold = `VPN_TRIGGER_THRESHOLD_USD`
- Cutover playbook (TBD when threshold approached)

## Fleet kill-switches
- Per-bot DD halts: see `bots/*/config.py`
- Fleet daily DD halt: -10%
- Fleet total DD kill: -25%
- Auto-resume: 48h after total kill, ONLY IF BTC moved <5% in prior 24h
- 8 consecutive losing trades → 24h pause + manual review

## Backups
- Postgres: Hetzner automated daily backup ($4/mo)
- Verification: monthly restore-to-scratch test (see `scripts/verify_backup.sh`)

## Cost line items (paper phase)
| Item | Monthly |
|------|---------|
| Hetzner CPX31 (or CPX41 if Ollama needs RAM) | $20-30 |
| Hetzner backup | $4 |
| Twilio (P0 SMS) | ~$5 |
| Helius Developer (Phase 2+) | $50 |
| Cielo Premium (Phase 2+) | $40 |
| Twitter API v2 Basic (Phase 3+) | $100 |
| Anthropic API ops review (Phase 3+) | ~$10 |
| Shadow execution real orders (Phase 1+) | $50-100 |
| **Total at full paper (4 bots)** | **~$280-340** |

## Deploy (auto-pull, 2026-05-25+)

Every push to `main` deploys to Hetzner within ~60s via `scripts/hetzner_autopull.sh`
running as a per-minute cron on the server. The script compares local
HEAD vs origin/main, `git reset --hard` on change, runs alembic if any
new migration files appear, and restarts only services whose code
actually changed.

**One-time setup on Hetzner** (already done if you see autopull.log on the server):
```
chmod +x ~/crypto-fleet/scripts/hetzner_autopull.sh
crontab -e
# add:
* * * * * /home/fleet/crypto-fleet/scripts/hetzner_autopull.sh >> ~/autopull.log 2>&1
```

**Pause auto-deploy** (e.g. during investigation):
```
touch ~/crypto-fleet/.autopull_paused
# (resume)
rm ~/crypto-fleet/.autopull_paused
```

**Manual-action flag** — set by the script when a docker-compose.yml,
Dockerfile, or requirements.txt change requires a rebuild:
```
ls ~/crypto-fleet/.autopull_manual_needed  # exists → manual rebuild pending
# After running rebuild:
docker compose build && docker compose up -d --force-recreate
rm ~/crypto-fleet/.autopull_manual_needed
```

**Restart-mapping** (which services restart for which path changes):
- `framework/scoring/`, `dd_monitor.py`, `kill_criteria_monitor.py`, `heartbeat.py` → `scoring`
- `framework/alembic/versions/` → run `migrate` then restart `scoring`
- `framework/{db,models,alerts,audit,config,...}.py` (shared) → restart everything
- `bots/structure/` → `bot_structure`
- `bots/copy/` → `bot_copy` + `bot_copy_webhook_receiver`
- `bots/base/` → both bots
- `monitoring/alerting/` → `alerting`
- `monitoring/dashboards/` → no restart (Grafana provisioning auto-reloads)
- `scripts/`, `tests/`, `memory/`, `*.md` → no restart
- `docker-compose.yml`, `Dockerfile`, `requirements.txt` → flag manual action

**Log**: `~/autopull.log`. Idle minutes produce no output; only deploys
write to it. To watch:
```
tail -f ~/autopull.log
```

## Service Reserve
- Initial seed: ~$2,520 (9 months runway) from Coinbase at deployment
- Steady-state target: ~$1,680 (6 months) refilled from bot profits
- Trigger: trailing 90d PnL negative AND reserve <3 months → funding decision alert

## Phase progression
1. Phase 0: Foundation Week — infra + monitoring + framework, no bots
2. Phase 1: STRUCTURE bot
3. Phase 2: COPY bot
4. Phase 3: EVENT bot
5. Phase 4: SNIPER bot
6. Phase 5: Promotion + L1/L2/L3 live deployment

Each phase ends with a shakedown gate (see plan file). Block next phase build until current gate passes.

## Reference docs
- Design state snapshot: `design_state_2026-04-26.md`
- Build plan: `C:\Users\Roy\.claude\plans\logical-scribbling-kernighan.md`

## Quarterly whale-list refresh (server-side cron)

The remote-agent approach failed (Anthropic cloud egress IP receives empty
responses from the Hyperliquid Info API). Replaced with a local cron on
Hetzner that runs the same logic from inside the framework container, where
the API works.

**Crontab entry** (as `fleet` user):
```
0 14 1 2,5,8,11 * cd /home/fleet/crypto-fleet && /usr/bin/docker compose exec -T framework python -m scripts.quarterly_whale_refresh >> /home/fleet/logs/whale_refresh.log 2>&1
```

Fires 14:00 UTC on the 1st of Feb / May / Aug / Nov.

**What the cron does**:
1. Fetches last-180d fills for every whale in `bots/structure/whale_list.json`
2. Recomputes win-rate / closed-position count / cumulative notional
3. Writes a `whale_refresh_completed` audit_log row with the full summary in payload
4. Emits a P2 Discord alert (or P1 if ALL whales would drop, which is suspicious)
5. **Does NOT modify whale_list.json** — Roy reviews and applies manually

**Manual review + apply** (after the cron fires):
1. Read the Discord summary alert, OR query Postgres:
   ```sql
   SELECT created_at, payload FROM audit_log
   WHERE event_type = 'whale_refresh_completed'
   ORDER BY id DESC LIMIT 5;
   ```
2. If kept whales look right and you want to apply changes:
   - Edit `bots/structure/whale_list.json` to remove dropped addresses (or use the dropped/kept list from audit payload as your guide)
   - Commit + push
   - Server `git pull` + `docker compose restart bot_structure`
3. If ALL whales dropped (P1 case): re-run the cron manually first to confirm it's not transient. Don't act until you see the same drops twice.

**Run manually for testing** (any time):
```bash
ssh fleet
cd ~/crypto-fleet
docker compose exec -T framework python -m scripts.quarterly_whale_refresh
```

**Re-discovery (separate flow)** — finding NEW whales beyond the existing
list — still requires manual SOCKS-proxied browser scrape per the original
documented procedure (see `project_phase_1_build_a.md` memory).
