#!/usr/bin/env bash
# Daily Postgres backup: pg_dump | gzip -> /var/backups/montaj (keeps the newest 14), and, when
# AZURE_BACKUP_CONTAINER is set in .env, a copy in that PRIVATE Blob container (needs the Azure CLI, installed
# by azure_setup.sh). Backups contain user data: keep them private.
#
# Cron example (03:15 every night; see deploy/README_DEPLOY.md):
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
  if command -v az >/dev/null; then
    az storage blob upload --only-show-errors --container-name "$container" --name "$(basename "$file")" \
      --file "$file" --connection-string "$(env_get AZURE_STORAGE_CONNECTION_STRING)" --output none
    echo "$(date -u +%FT%TZ) uploaded to blob container $container"
  else
    echo "AZURE_BACKUP_CONTAINER is set but 'az' is not installed: local copy only" >&2
  fi
fi
