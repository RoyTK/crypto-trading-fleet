## Deployment & Infrastructure

_Last reviewed: 2026-07-02_

### Topology

One Hetzner VPS runs everything via `docker-compose.yml`: `postgres`, `redis`,
`prometheus`, `grafana`, `framework`, `scoring`, `alerting`, `report_cron`, `bot_copy`,
`bot_copy_webhook_receiver`, and a one-shot `migrate` (profile `tools`). (`bot_structure`
was removed from compose on 2026-06-25 — STRUCTURE is decommissioned.) A single
`framework/Dockerfile` builds the Python image used by all app services.

### How code reaches the server (auto-pull deploy)

A cron on the server runs `scripts/hetzner_autopull.sh` **every minute**. It:
1. `git fetch origin main`; if `origin/main` is ahead, `git reset --hard origin/main`.
2. Detects changed files and restarts only affected services (mapping below).
3. Runs `alembic upgrade head` if new migration files appeared.

So the normal deploy is just: **commit and push to `main`** → live within ~60s.

> **Consequence — untracked files get wiped.** `reset --hard` discards anything not
> committed. Files the bot needs must be **committed**, not `scp`-ed in. (This is why the
> vetting results live in the repo.)

**Changed-file → restart mapping (approximate):**
| Changed path | Restarts |
|---|---|
| `bots/copy/**` | `bot_copy` + `bot_copy_webhook_receiver` |
| `bots/structure/**` | *(no-op — `bot_structure` is decommissioned/removed from compose)* |
| `framework/scoring/`, `kill_criteria_monitor.py`, etc. | `scoring` |
| `framework/alembic/versions/**` | run migrations, then `scoring` |
| shared framework (`models.py`, `db.py`, `alerts.py`, …) | all services |
| `monitoring/alerting/**` | `alerting` |
| `monitoring/dashboards/**` | nothing (Grafana auto-reloads) |
| `scripts/**`, `tests/**`, `*.md`, `docs/**`, `memory/**` | nothing |

### Canonical manual deploy (when you must rebuild)

Auto-pull does NOT handle changes to `docker-compose.yml`, the `Dockerfile`, or
`requirements.txt`, and it does NOT re-read `.env`. For those, on the server:

```bash
cd ~/crypto-fleet && git pull origin main && \
  docker compose build <services...> && \
  docker compose up -d --force-recreate <services...>
```

Common full form used this project:
```bash
cd ~/crypto-fleet && git pull origin main && \
  docker compose build framework bot_copy scoring && \
  docker compose up -d --force-recreate framework bot_copy scoring
```

> **`.env` gotcha:** `docker compose restart` does **not** re-read `.env`. After editing
> `.env` you must `docker compose up -d --force-recreate <service>` and verify with
> `docker compose exec <service> printenv VAR`.

### Pausing auto-deploy (for debugging)

```bash
touch ~/crypto-fleet/.autopull_paused   # pause
rm    ~/crypto-fleet/.autopull_paused   # resume
tail -f ~/autopull.log                  # watch deploys
```

### Migrations (Alembic)

Schema is versioned in `framework/alembic/versions/`. Auto-pull runs `alembic upgrade head`
when a new migration lands. Manually: `docker compose run --rm migrate`. To add one, write
a revision, test locally, commit — it deploys and applies automatically.

### Fresh-server setup (rebuilding from nothing)

If you must stand up a new host:
1. Provision a Hetzner VPS (CPX32-class), add your SSH key, harden SSH.
2. Install Docker + Docker Compose plugin and `git`.
3. `git clone` the repo to `~/crypto-fleet`.
4. Create `~/crypto-fleet/.env` from `.env.example` and fill **every** secret (from the
   Access Sheet / your offline `.env` printout). This is the critical, manual step.
5. `docker compose build && docker compose up -d` (postgres/redis first via healthchecks).
6. `docker compose run --rm migrate` to create the schema.
7. Re-create the Helius webhooks: `docker compose exec framework python -m scripts.helius_webhook_setup`.
8. Re-install the server crontab (see *Operations (technical)* for the lines), including
   `hetzner_autopull.sh` every minute.
9. Re-establish remote access (Cloudflare Tunnel — see `monitoring/cloudflared/README.md`).
10. Confirm Discord/Grafana/heartbeats are green.

### Local development

- `cp .env.example .env` and fill at least the DB/Redis vars (external API keys optional
  for unit tests).
- `docker compose up -d postgres redis` for a local DB, or point at a throwaway DB.
- Run tests: `python -m pytest` (e.g. `tests/test_wallet_pool_manager.py`).
- Most logic (tier decisions, signals) is unit-testable without live services — prefer
  that over hitting real APIs.
