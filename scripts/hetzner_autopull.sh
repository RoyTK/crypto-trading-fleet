#!/usr/bin/env bash
# Hetzner auto-pull deploy script.
#
# Runs every minute via cron. Compares local HEAD to origin/main; if behind,
# fast-forwards, runs migrations if any new alembic files appeared, and
# restarts only the services whose code actually changed.
#
# Safe to interrupt — every step is idempotent. Bails silently if no work
# to do (so cron output is quiet on idle minutes).
#
# Pause for manual intervention:
#   touch ~/crypto-fleet/.autopull_paused
# Resume:
#   rm ~/crypto-fleet/.autopull_paused
#
# Cron entry (run once on server to enable):
#   * * * * * /home/fleet/crypto-fleet/scripts/hetzner_autopull.sh >> ~/autopull.log 2>&1

set -uo pipefail

REPO_DIR="/home/fleet/crypto-fleet"
PAUSE_FLAG="$REPO_DIR/.autopull_paused"
MANUAL_FLAG="$REPO_DIR/.autopull_manual_needed"
TS() { date -u +%Y-%m-%dT%H:%M:%SZ; }

cd "$REPO_DIR" 2>/dev/null || {
    echo "[$(TS)] autopull FATAL: cannot cd to $REPO_DIR"
    exit 1
}

# Pause flag — bail silently. No log noise; operator knows it's paused.
if [ -f "$PAUSE_FLAG" ]; then
    exit 0
fi

# Fetch quietly. Transient network failures shouldn't make noise.
if ! git fetch --quiet origin main 2>/dev/null; then
    # Only log once per hour to avoid spam if persistent (compare against minute)
    if [ "$(date -u +%M)" = "00" ]; then
        echo "[$(TS)] autopull WARN: git fetch failed (transient network?)"
    fi
    exit 0
fi

local_head=$(git rev-parse HEAD)
remote_head=$(git rev-parse origin/main)

# No change → exit silently
if [ "$local_head" = "$remote_head" ]; then
    exit 0
fi

echo "[$(TS)] autopull deploying $local_head -> $remote_head"

# Reset to origin/main. Hetzner has no local commits to preserve; reset is
# safer than pull for an automated deploy (resilient to any local mess).
if ! git reset --hard "$remote_head" 2>&1; then
    echo "[$(TS)] autopull FATAL: reset --hard failed"
    touch "$MANUAL_FLAG"
    exit 1
fi

changed_files=$(git diff --name-only "$local_head" "$remote_head")

restart_scoring=false
restart_structure=false
restart_copy=false
restart_alerting=false
restart_report_cron=false
restart_framework=false
need_migrate=false
need_manual=false
shared_framework_touched=false

while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
        framework/alembic/versions/*)
            need_migrate=true
            restart_scoring=true
            ;;
        framework/scoring/*|framework/dd_monitor.py|framework/kill_criteria_monitor.py|framework/heartbeat.py)
            restart_scoring=true
            ;;
        framework/main.py|framework/watchdog.py)
            # Supervisor process — runs in the `framework` container (heartbeat watchdog,
            # reconciliation loop, RB3/RB4 alert paths). Autopull historically never
            # restarted `framework` under ANY condition, so watchdog/supervisor changes
            # silently did NOT deploy (had to manually `docker compose restart framework`).
            # Fixed 2026-07-18.
            restart_framework=true
            ;;
        framework/audit.py|framework/db.py|framework/models.py|framework/alerts.py|framework/alert_emit.py|framework/config.py|framework/logging_setup.py|framework/halt_state.py|framework/reconciliation.py|framework/__init__.py)
            shared_framework_touched=true
            ;;
        bots/structure/*)
            restart_structure=true
            ;;
        bots/copy/*)
            restart_copy=true
            ;;
        bots/base/*)
            restart_structure=true
            restart_copy=true
            ;;
        monitoring/alerting/*)
            restart_alerting=true
            ;;
        monitoring/dashboards/*)
            # Grafana auto-reloads from mounted dir; no restart needed.
            ;;
        docker-compose.yml|framework/Dockerfile|*.dockerfile|Dockerfile*|framework/requirements.txt)
            need_manual=true
            ;;
        scripts/*|tests/*|memory/*|*.md|.env.example|.gitignore)
            # Operator/doc/test changes — no restart implication.
            ;;
        *)
            # Unknown path — log it but proceed without restart. If it turns
            # out to be important, operator sees it and updates the script.
            echo "[$(TS)] autopull UNKNOWN PATH: $f"
            ;;
    esac
done <<< "$changed_files"

if [ "$shared_framework_touched" = "true" ]; then
    # Shared framework code touched → restart everything that imports framework.
    restart_framework=true
    restart_scoring=true
    restart_structure=true
    restart_copy=true
    restart_alerting=true
    restart_report_cron=true
fi

if [ "$need_manual" = "true" ]; then
    echo "[$(TS)] autopull MANUAL ACTION REQUIRED: docker-compose / Dockerfile / requirements changed"
    echo "[$(TS)]   Run: docker compose build && docker compose up -d --force-recreate"
    touch "$MANUAL_FLAG"
    # Proceed with restarts of services that don't need rebuild.
fi

if [ "$need_migrate" = "true" ]; then
    echo "[$(TS)] autopull running migrate..."
    docker compose run --rm migrate 2>&1 | sed "s/^/  [migrate] /"
fi

_restart() {
    local svc=$1
    echo "[$(TS)] autopull restart $svc"
    docker compose restart "$svc" 2>&1 | sed "s/^/  [$svc] /"
}

[ "$restart_framework" = "true" ]   && _restart framework
[ "$restart_scoring" = "true" ]     && _restart scoring
[ "$restart_structure" = "true" ]   && _restart bot_structure
[ "$restart_copy" = "true" ]        && { _restart bot_copy; _restart bot_copy_webhook_receiver; }
[ "$restart_alerting" = "true" ]    && _restart alerting
[ "$restart_report_cron" = "true" ] && _restart report_cron

echo "[$(TS)] autopull deploy complete: $remote_head"
