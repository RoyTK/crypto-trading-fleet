## Troubleshooting

_Last reviewed: 2026-07-02_

Symptom → likely cause → fix. Start with the symptom you see in Discord/logs.

### "A discovery pass added 0 rows" / Discord shows ⚠ 0-rows
- **Cause:** the browser session wasn't actually driving Birdeye — usually the Chrome
  extension's service worker went idle, or a Cloudflare/login wall appeared.
- **Fix:** re-open/reconnect Chrome (and `/chrome` if using the CLI), confirm Birdeye is
  logged in, re-run. The file-as-checkpoint means completed batches were already saved.

### "claude is not recognized" (in a scheduled/script run)
- **Cause:** the Claude Code CLI isn't on that process's PATH (Task Scheduler launches with
  a stale env), or it isn't installed as a standalone CLI.
- **Fix:** install/locate the CLI (`(Get-Command claude).Source`) and hardcode the path; but
  note the headless browser path doesn't work anyway — run discovery **attended**.

### "Heartbeat silent" / a service went quiet (P1)
- **Cause:** a container crashed or wedged.
- **Fix:** `docker compose ps`; `docker compose logs --tail=100 <svc>`;
  `docker compose up -d <svc>` (or `--force-recreate`). The watchdog also auto-tries a
  restart; a P0 follows if that fails.

### "kill criterion fired" (P1)
- **Cause:** a strategy metric (win rate, consecutive losses, Sharpe) crossed a threshold.
- **Fix:** **none required automatically** — it's informational. Note it; a human decides
  later whether to continue/halt/extend. Do **not** start tuning config in response (resets
  the window).

### "Helius credits spiking" / approaching the 30M cap
- **Cause:** too many or too-active subscribed wallets (often an MM/HFT wallet slipped in).
- **Fix:** check `credit_pool_snapshot` (top heavy-hitters) and the Helius dashboard; prune
  the offender, slow discovery, or raise the budget. Remember both active + watch cost
  credits.

### "Helius webhook 400 / sync errors"
- **Cause:** address-list churn or a malformed sync.
- **Fix:** `docker compose exec -T framework python -m scripts.helius_webhook_setup --list`
  to inspect; re-run the daily sync; check `HELIUS_*` env vars are set.

### "A trade booked a fake profit on a rugged token"
- **Cause:** a pre-fix orphan, or a stale price at close.
- **Fix:** the rug check now prevents new cases; for a historical one, use
  `scripts/correct_rug_trade.py` (targeted, audited) — do **not** bulk-edit trades.

### "I changed `.env` but nothing happened"
- **Cause:** `restart` doesn't re-read `.env`.
- **Fix:** `docker compose up -d --force-recreate <svc>`; verify `docker compose exec <svc> printenv VAR`.

### "I changed the conviction/team-follow roster but nothing changed"
- **Cause:** rosters are read **only at `bot_copy` startup**.
- **Fix:** `docker compose restart bot_copy` after the roster file lands, then confirm in the
  logs that it reloaded the expected wallet count.

### "A deploy broke something"
- **Fix:** `git revert <commit> && git push` — auto-pull rolls it forward in ~60s. Or
  `touch ~/crypto-fleet/.autopull_paused` to freeze while you investigate.

### "Helius dashboard won't load"
- **Cause:** not tunneling through the server (account is German-IP bound), or the proxy is
  still on after you finished.
- **Fix:** use `ssh -D 9999 -N fleet hetzner.com` + Firefox SOCKS5 `127.0.0.1:9999`; verify
  via `ifconfig.me`; turn the proxy off afterward.

### Known non-obvious gotchas (catalog)
The full, evolving list of subtle pitfalls lives in `memory/project_ops_gotchas.md`
(e.g. container runs *baked* code so data files need `docker compose cp`; auto-pull wipes
untracked files; `restart` ignores `.env`; PowerShell 5.1 mangles non-ASCII in `.ps1`).
When something weird happens, check there.
