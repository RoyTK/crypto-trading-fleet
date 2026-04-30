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
