# Ops cheatsheet

Roy-facing quick reference. Things to know without re-reading whole docs.
Updated 2026-05-25.

---

## Auto-deploy (Hetzner)

Every `git push` to `main` deploys to Hetzner within ~60s. Script:
`scripts/hetzner_autopull.sh` runs as a per-minute crontab entry.

**The cron line** (in case you ever lose it — run `crontab -e` to add):
```
* * * * * /home/fleet/crypto-fleet/scripts/hetzner_autopull.sh >> ~/autopull.log 2>&1
```

**Pause auto-deploy** (useful when debugging mid-incident):
```bash
touch ~/crypto-fleet/.autopull_paused
# (resume)
rm ~/crypto-fleet/.autopull_paused
```

**Manual-action flag** — script touches this when something needs a
rebuild (docker-compose.yml, Dockerfile, or requirements.txt changed):
```bash
ls ~/crypto-fleet/.autopull_manual_needed   # exists → run:
docker compose build && docker compose up -d --force-recreate
rm ~/crypto-fleet/.autopull_manual_needed
```

**Watch deploys live:** `tail -f ~/autopull.log` (idle minutes are silent — that's correct).

---

## Kill / promotion criteria window

**Window:** 2026-05-25 → 2026-07-24 primary (auto-extends to 2026-08-23 if N target not reached).

**Where to see status:** Grafana → STRUCTURE Detail (or COPY Detail) → scroll to bottom row "Kill / promotion criteria". Shows days remaining, N, WR, PnL %, any active triggers.

**Where the actual criteria are signed:** `memory/project_decision_log.md` entry dated 2026-05-25.

**Critical:** Alerts only. No auto-halt. You keep the manual kill/promote decision. If a P1 alert fires titled "[structure] kill criterion fired: ...", that's a heads-up, not an automatic action.

**What resets the window if you change it** (configs in `bots/structure/config.py` and `bots/copy/config.py`):
- Entry/exit logic, signal-filtering, sizing constants, risk gates → RESETS window
- Bug fixes, dashboards, monitoring, logging → does NOT reset
- A data-driven correction can also avoid reset, but only if (1) you write the justification in audit_log BEFORE the change, (2) it names which observation forced it, (3) the change shrinks behavior space rather than expands, (4) at most once per parameter per window

---

## Common ops commands

```bash
# SSH in
ssh fleet@<hetzner-ip>
cd ~/crypto-fleet

# What's running
docker compose ps

# Tail logs for a service
docker compose logs -f --tail=50 scoring
docker compose logs -f --tail=50 bot_structure
docker compose logs -f --tail=50 bot_copy

# Restart a single service (rarely needed if autopull is on)
docker compose restart scoring

# Query bot state
docker compose exec postgres psql -U fleet -d fleet -c \
  "SELECT bot_id, state, paper_clock_started_at FROM bot_state;"

# Query current kill-criteria snapshot
docker compose exec postgres psql -U fleet -d fleet -c \
  "SELECT bot_id,
          kill_criteria_status->'window'->>'days_remaining_primary' AS days,
          kill_criteria_status->>'n' AS n,
          kill_criteria_status->>'wr' AS wr,
          kill_criteria_status->>'net_pnl_pct' AS pnl_pct,
          kill_criteria_status->>'paper_capital_usd' AS capital
   FROM bot_state WHERE bot_id IN ('structure','copy');"

# Run a one-shot alembic upgrade (if a migration didn't auto-apply)
docker compose run --rm migrate
```

---

## Important files on Hetzner

| File | Why it matters |
|---|---|
| `~/crypto-fleet/.env` | Capital, API keys, secrets. Not in git. Edit carefully. |
| `~/crypto-fleet/.autopull_paused` | Touch to halt auto-deploy. |
| `~/crypto-fleet/.autopull_manual_needed` | Script touched this; rebuild required. |
| `~/autopull.log` | Auto-deploy log. Idle minutes = no output. |
| `~/crypto-fleet/scripts/hetzner_autopull.sh` | The deploy script itself. |

---

## Important files in the repo

| File | Purpose |
|---|---|
| `OPERATIONS.md` | Full operations doc — phases, costs, deploy procedure. |
| `memory/project_decision_log.md` | Signed design decisions (incl. kill criteria). |
| `bots/structure/config.py` + `bots/copy/config.py` | Window-locked constants live here. Window-reset rules in the file header. |
| `framework/kill_criteria_monitor.py` | Where the alert thresholds + window dates live (hardcoded constants at top). |
| `monitoring/dashboards/structure-detail.json` + `copy-detail.json` | Grafana panel definitions. |

---

## Paid services (current, ~$165/mo total)

| Service | $/mo | Used for |
|---|---|---|
| Helius Developer | $24.50 | Solana RPC + webhooks (COPY) |
| Cielo Pro | $65 | Wallet curation + PnL tracking (COPY) |
| Birdeye Lite | $19 | Token pricing + wallet discovery (COPY) |
| Hetzner CPX32 | $25 | Bot host |
| Coinglass Hobbyist | $35 | (planned to lapse — Hobbyist tier blocks 5min liq intervals; STRUCTURE liq_cascade gated off via STRUCTURE_LIQ_CASCADE_ENABLED=false) |

**Not available:** X/Twitter API (Roy doesn't have paid tier), Twilio (no SMS alerts), colocation (no sub-30ms execution venues).

---

## Quick sanity check: "is the fleet healthy?"

Run this from your laptop or SSH:
```bash
ssh fleet@<hetzner-ip> '
  cd ~/crypto-fleet &&
  docker compose ps --format json | jq -r ".Name + \" \" + .State" &&
  echo "---" &&
  tail -3 ~/autopull.log 2>/dev/null
'
```
Should show all services `running` and an empty or recent autopull log.

If something looks off: `docker compose logs --tail=100 <service-that-is-not-running>` to see why.
