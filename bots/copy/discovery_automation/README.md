# COPY wallet discovery + vetting automation

Scheduled browser run that **discovers and vets** Solana trader wallets for the COPY
bot in one pass, appending verdicts to the wallet-pool vetting ledger. Runs on Claude
Code (headless `claude -p`, Opus, Max quota) driven by Windows Task Scheduler.

## Files
- `browser_discovery_vetting_prompt.txt` — the prompt each scheduled run executes.
  Browser = Birdeye **discovery only**; output = the local results file via file tools.
- `run_wallet_discovery.ps1` — wrapper. Clears `ANTHROPIC_API_KEY` (forces Max billing),
  runs `claude -p` on Opus, then: **row-count health check** → **git commit/push** of
  the results file → **Discord signal** (`OK +N` / `WARN 0-rows` / `FAIL`).
- `register_task.ps1` — run ONCE elevated to create the task. Every **13 hours, drifting**.
- `Btowser wallet curation prompt.txt` — legacy discovery-only prompt (reference only).
- `logs/` — per-run JSON + err logs and the single-instance lock (created on first run).

## ⚠ Path constraint — the results file does NOT live here
`vetted_watch_results.txt` stays in the **parent** dir (`bots/copy/`), because the
Hetzner daily cron `docker compose cp`s it from that path and `apply_vetting_results`
ingests it. The wrapper writes there and runs `claude` with **cwd = `bots/copy`** so the
prompt's "vetted_watch_results.txt in your working dir" resolves. **Do not move it.**

## Setup (once)
1. Discord webhook for the health signal:
   `[Environment]::SetEnvironmentVariable('COPY_DISCORD_WEBHOOK','<url>','User')`
2. Register the task (elevated PowerShell): `.\register_task.ps1`
   (it verifies the 13h repetition stuck — Task Scheduler sometimes drops it).
3. Test: `Start-ScheduledTask -TaskName CryptoWalletDiscovery`, watch `logs\` + Discord.

## Schedule
Every **13 hours**, drifting. 13 is coprime to 24, so over ~13 days the run time samples
**every hour of the day**, catching traders active in all timezones — vs a fixed time
that always sees the same two clock-moments. Marginal coverage gain, ~zero cost.

## Pipeline (fully closed loop)
```
browser discovery → append to bots/copy/vetted_watch_results.txt → wrapper git push →
Hetzner autopull → 07:30 cron apply_vetting_results
  KEEP → source=browser_opus_vetted (vetted-only promotion → active)
  REJECT → tier=pruned
  TOO_FAST → logged only (sub-15min, unfollowable by COPY latency)
```

## Health / failure modes
- **✅ +N rows** — worked, loop closed.
- **⚠ 0 rows** — clean exit, nothing appended. Usually the Chrome extension service
  worker went idle (no headless / no auto-reconnect) or a Cloudflare/login wall.
  Reconnect Chrome (`/chrome`). This is the most likely failure over long idle gaps.
- **❌ FAILED** — check the newest `logs/run_*.err.log`.

## Trust note (revisit before live money)
KEEP rows auto-ingest into a bot that places trades, built from untrusted page content.
Acceptable while COPY is paper (base58 validation + per-run token cap + downstream
vetted-only promotion / rug checks). **When COPY goes live-money:** commit discovery
output to a branch and merge after a human glance, instead of auto-pushing to `main`.
