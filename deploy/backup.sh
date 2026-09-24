#!/usr/bin/env bash
# Daily Postgres backup: pg_dump | gzip -> /var/backups/montaj (keeps the newest 14), and, when
# AZURE_BACKUP_CONTAINER is set in .env, a copy in that PRIVATE Blob container (uploaded from inside the api
# image: no Azure CLI needed). Backups contain user data: keep them private.
#
# ./install.sh installs the nightly cron job for you (03:15). Manual example:
#   15 3 * * * cd /home/azureuser/montaj-bot && ./deploy/backup.sh >> /var/log/montaj-backup.log 2>&1
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml)
BACKUP_DIR="${BACKUP_DIR:-/var/backups/montaj}"
KEEP="${KEEP:-14}"
env_get() { grep -E "^$1=" .env 2>/dev/null | head -n1 | cut -d= -f2- || true; }   # never `source .env`: values contain ';'

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
file="$BACKUP_DIR/montaj_${stamp}.sql.gz"

echo "$(date -u +%FT%TZ) dumping to $file"
"${COMPOSE[@]}" exec -T db pg_dump -U montaj --no-owner montaj | gzip > "$file.part"
[[ -s "$file.part" ]] || { rm -f "$file.part"; echo "backup is empty: aborting" >&2; exit 1; }
gzip -t "$file.part"        # the archive must be readable before it replaces anything
mv "$file.part" "$file"
echo "$(date -u +%FT%TZ) wrote $(du -h "$file" | cut -f1)"

# keep the newest $KEEP local backups
ls -1t "$BACKUP_DIR"/montaj_*.sql.gz | tail -n +"$((KEEP + 1))" | xargs -r rm -f

container="$(env_get AZURE_BACKUP_CONTAINER)"
if [[ -n "$container" ]]; then
  # Upload from inside the api image (no Azure CLI needed on the server). A failed upload must not fail the
  # backup: the local copy above is already safe.
  "${COMPOSE[@]}" run --rm -T -v "$file:/backup/dump.sql.gz:ro" api \
    python scripts/upload_backup.py /backup/dump.sql.gz "$(basename "$file")" \
    && echo "$(date -u +%FT%TZ) uploaded to blob container $container" \
    || echo "$(date -u +%FT%TZ) WARNING: blob upload failed: local copy only" >&2
fi
