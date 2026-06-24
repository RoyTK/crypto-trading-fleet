## Disaster Recovery

_Last reviewed: 2026-06-24_

### What can break, and how bad it is

| Scenario | Severity | Recoverable? |
|---|---|---|
| A container crashed / went silent | Low | Yes — watchdog tries to restart; else `docker compose up -d <svc>` |
| Bad deploy | Low | Yes — revert the commit and push; auto-pull rolls forward |
| Database lost | High | Yes if backups exist — restore (below). Trade history is the main loss |
| Whole server lost | High | Yes — rebuild a fresh server (see *Deployment → Fresh-server setup*) + restore DB |
| `.env` / secrets lost | **Critical** | Only if you have the offline copy — **this is the one thing git cannot recover** |

The single most important disaster-prep fact: **secrets are not in git.** If the server and
your offline `.env`/Access Sheet are both gone, the API keys and wallet keys are gone. Keep
the printed Access Sheet and a printed/encrypted `.env` somewhere safe and separate.

### Restore the database

Hetzner provides automated daily VPS snapshots/backups (enable in the Hetzner console; ~$4/mo).
To recover:
1. Restore the VPS from the most recent Hetzner snapshot **or** stand up a fresh server and
   restore just Postgres from a `pg_dump` if you keep one.
2. If using a logical dump: `docker compose up -d postgres`, then
   `cat backup.sql | docker compose exec -T postgres psql -U fleet -d fleet` (include an
   explicit `COMMIT;` in SQL files — session-EOF behavior is unreliable here).
3. Bring up the rest: `docker compose up -d`.
4. Re-sync Helius webhooks: `docker compose exec framework python -m scripts.helius_webhook_setup`.
5. Verify: `docker compose exec postgres psql -U fleet -d fleet -c "SELECT count(*) FROM trades;"`
   and confirm heartbeats are fresh.

> Recommended hygiene: a monthly "restore to scratch" test so you *know* the backup works.

### Rebuild the whole server

Follow **Deployment → Fresh-server setup**. The only inputs you cannot regenerate are the
**secrets** (`.env`) — everything else (code, schema, dashboards) is in git. Budget a few
hours; the gating step is filling `.env` correctly from the Access Sheet.

### Rotate a leaked credential

If an API key or webhook secret leaks: rotate it in that service's console (Access Sheet
has the URLs), update `.env` on the server, `docker compose up -d --force-recreate <svc>`,
and for Helius re-run `helius_webhook_setup`. Never put the new secret in git.

### "I don't know what's wrong" — safe default

1. `/panic` in Discord (halts all trading; loses nothing — it's paper).
2. `docker compose ps` and `docker compose logs --tail=100 <noisy service>`.
3. If a deploy caused it, `git revert` the last commit and push.
4. Call the technical contact. A halted, paper-only system is in no danger sitting still.
