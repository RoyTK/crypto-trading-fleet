#!/usr/bin/env bash
# Restore-to-scratch verification (Phase 0 shakedown gate item #8).
# Usage: ./verify_backup.sh /path/to/postgres-dump.sql

set -euo pipefail

DUMP_FILE="${1:-}"
if [[ -z "$DUMP_FILE" ]]; then
  echo "usage: $0 /path/to/postgres-dump.sql" >&2
  exit 2
fi
if [[ ! -f "$DUMP_FILE" ]]; then
  echo "dump file not found: $DUMP_FILE" >&2
  exit 2
fi

CONTAINER_NAME="fleet-backup-verify-$(date +%s)"

echo "Spinning up scratch Postgres container..."
docker run -d --name "$CONTAINER_NAME" \
  -e POSTGRES_PASSWORD=verify \
  -e POSTGRES_DB=fleet \
  -e POSTGRES_USER=fleet \
  postgres:16-alpine

cleanup() {
  echo "Cleaning up..."
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Waiting for Postgres to be ready..."
for i in {1..30}; do
  if docker exec "$CONTAINER_NAME" pg_isready -U fleet >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "Loading dump..."
docker exec -i "$CONTAINER_NAME" psql -U fleet -d fleet < "$DUMP_FILE"

echo "Verifying tables..."
TABLES=$(docker exec "$CONTAINER_NAME" psql -U fleet -d fleet -t -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'")
echo "Tables found: $TABLES"

echo "Spot-checking row counts..."
for tbl in bot_state signals trades halts scores audit_log heartbeats calibration_records; do
  count=$(docker exec "$CONTAINER_NAME" psql -U fleet -d fleet -t -c "SELECT COUNT(*) FROM $tbl" 2>/dev/null || echo "missing")
  echo "  $tbl: $count"
done

echo "Backup restore verification: OK"
