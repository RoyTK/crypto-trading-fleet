# Crypto Trading Bot Fleet

A fleet of 4 deterministic crypto trading bots (STRUCTURE, COPY, EVENT, SNIPER) competing in 8-week paper trade for live capital allocation.

**Status**: Phase 0 (Foundation Week) — building infrastructure, no bots yet.

## Quick links
- [Design state snapshot](design_state_2026-04-26.md) — full design decisions across all 7 agenda items
- [Operations runbook](OPERATIONS.md) — alerts, /panic, kill-switches, cost lines

## Phase 0 layout

```
.
├── docker-compose.yml          # Postgres, Redis, Prometheus, Grafana
├── .env.example                # secrets template (copy to .env)
├── framework/                  # shared bot infra
│   ├── alembic/                # schema migrations
│   ├── scoring/                # PromotionScore engine (separate process)
│   └── reporting/              # 7am daily check-in generator
├── monitoring/
│   ├── alerting/               # Discord, Telegram, Twilio routers
│   ├── dashboards/             # Grafana JSON
│   └── prometheus/             # scrape config
├── bots/                       # one folder per bot (added in Phase 1+)
├── scripts/                    # one-off ops scripts
└── tests/
```

## Running locally (Phase 0)

1. Copy `.env.example` to `.env` and fill in secrets
2. `docker compose up -d`
3. `docker compose exec framework alembic upgrade head`
4. Verify services: Postgres :5432, Redis :6379, Grafana :3000, Prometheus :9090

## Phase 0 shakedown checklist
See [the build plan](https://github.com/anthropics/claude-code) (local: `C:\Users\Roy\.claude\plans\logical-scribbling-kernighan.md`):
1. Fake-signal end-to-end
2. /panic on Discord + Telegram
3. Heartbeat self-restart
4. P0 SMS via Twilio
5. Empty-fleet daily report at 7am
6. `alembic upgrade head` clean on fresh DB
7. Cloudflare Tunnel auth from non-VPN
8. Backup restore verified
