#!/usr/bin/env bash
# Nightly dump of the MoneyMate database, kept for 14 days.
#
#   Installed by cron on the server (see DEPLOYMENT.md):
#   15 2 * * * /home/ubuntu/moneymate-backend/scripts/backup_postgres.sh
#
# Writes backups/moneymate-YYYYMMDD-HHMMSS.sql.gz beside docker-compose.yml.
# A dump that fails, or comes out empty, is deleted and the script exits
# non-zero, so cron mails the failure instead of keeping a useless file.
#
# Restore (into an EMPTY database):
#   gunzip -c backups/<file>.sql.gz | docker compose exec -T db psql -U moneymate -d moneymate
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p backups
chmod 700 backups

stamp=$(date -u +%Y%m%d-%H%M%S)
target="backups/moneymate-${stamp}.sql.gz"

if ! docker compose exec -T db pg_dump -U "${POSTGRES_USER:-moneymate}" -d "${POSTGRES_DB:-moneymate}" --no-owner \
    | gzip -9 > "$target"; then
  rm -f "$target"
  echo "backup FAILED" >&2
  exit 1
fi

if [ "$(gzip -cd "$target" | head -c 100 | wc -c)" -lt 100 ]; then
  rm -f "$target"
  echo "backup came out empty - removed" >&2
  exit 1
fi
chmod 600 "$target"

# Keep two weeks.
find backups -name 'moneymate-*.sql.gz' -mtime +14 -delete
echo "backup ok: $target ($(du -h "$target" | cut -f1))"
