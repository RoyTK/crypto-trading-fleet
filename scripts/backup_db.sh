#!/usr/bin/env bash
# Nightly Postgres logical backup (RB1, 2026-07-18). On-server copy (protects against
# DB corruption / bad migration / accidental drop); the Windows CryptoFleet-BackupDB
# task mirrors the latest to OneDrive for the OFFSITE copy (VPS-loss protection). Keeps
# 14 days on-server. Run by host cron. The 2.5-month trades/signals dataset is the
# project's single hardest-to-recreate asset — this is its safety net.
set -euo pipefail

BACKUP_DIR="${FLEET_BACKUP_DIR:-/home/fleet/backups}"
COMPOSE_DIR="${FLEET_COMPOSE_DIR:-/home/fleet/crypto-fleet}"
MIN_BYTES="${FLEET_BACKUP_MIN_BYTES:-1000000}"   # a valid dump is tens of MB; <1MB = broken
KEEP_DAYS="${FLEET_BACKUP_KEEP_DAYS:-14}"

mkdir -p "$BACKUP_DIR"
OUT="$BACKUP_DIR/fleet_db_$(date +%Y%m%d_%H%M).sql.gz"

cd "$COMPOSE_DIR"
docker compose exec -T postgres pg_dump -U fleet -d fleet | gzip > "$OUT"

# Sanity: fail LOUD if the dump is suspiciously small (broken pg_dump / empty DB).
SZ=$(stat -c%s "$OUT")
if [ "$SZ" -lt "$MIN_BYTES" ]; then
  echo "$(date -u +%FT%TZ) BACKUP FAILED: $OUT is only $SZ bytes (< $MIN_BYTES)" >&2
  exit 1
fi

# Integrity: the gzip must decompress cleanly.
if ! gzip -t "$OUT"; then
  echo "$(date -u +%FT%TZ) BACKUP CORRUPT: $OUT failed gzip -t" >&2
  exit 1
fi

find "$BACKUP_DIR" -name 'fleet_db_*.sql.gz' -mtime +"$KEEP_DAYS" -delete
echo "$(date -u +%FT%TZ) ok $OUT ($SZ bytes)"
