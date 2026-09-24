#!/usr/bin/env bash
# Deploys the current code on the VM. Run it ON the VM from the project directory, or from your laptop:
#
#   ./deploy/deploy.sh                         # on the VM
#   DEPLOY_HOST=azureuser@1.2.3.4 ./deploy/deploy.sh   # from anywhere: runs the same script over ssh
#
# Steps: git pull -> build the image once -> alembic upgrade head -> blob containers + CORS -> up -d (api, bot,
# worker, db, redis, telegram-bot-api, caddy) -> show containers and tail the logs for 10 s -> health check.
# First-time setup of a fresh server: use ./install.sh (it prepares .env and Docker, then runs this script).
set -euo pipefail

if [[ -n "${DEPLOY_HOST:-}" ]]; then
  exec ssh "$DEPLOY_HOST" "cd ${DEPLOY_DIR:-~/montaj-bot} && ./deploy/deploy.sh"
fi

cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml)

die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
env_get() { grep -E "^$1=" .env | head -n1 | cut -d= -f2- || true; }   # never `source .env`: values contain ';'

[[ -f .env ]] || die ".env is missing: copy .env.example to .env and fill in the real values"
for key in BOT_TOKEN PUBLIC_DOMAIN POSTGRES_PASSWORD AZURE_STORAGE_CONNECTION_STRING PUBLIC_BASE_URL DATABASE_URL; do
  [[ -n "$(env_get "$key")" ]] || die "$key is empty in .env"
done
[[ "$(env_get PUBLIC_BASE_URL)" == https://* ]] || die "PUBLIC_BASE_URL must be https://<PUBLIC_DOMAIN> (Telegram only opens HTTPS Mini Apps)"
[[ "$(env_get AZURE_STORAGE_CONNECTION_STRING)" != *devstoreaccount1* ]] || die "the storage connection string still points at Azurite"

echo "==> git pull"
if [[ -n "${SKIP_PULL:-}" ]]; then
  echo "(skipped: the caller already updated the code)"
elif git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
  git pull --ff-only
else
  echo "(no git remote configured for this checkout: deploying the code that is here)"
fi

echo "==> build (one image for api, bot and worker)"
"${COMPOSE[@]}" build api

echo "==> database migrations (BEFORE the new containers start: new code may query new tables)"
"${COMPOSE[@]}" run --rm api alembic upgrade head

echo "==> blob containers + CORS (idempotent)"
"${COMPOSE[@]}" run --rm api python scripts/configure_storage_cors.py

echo "==> start"
"${COMPOSE[@]}" up -d --remove-orphans

echo "==> containers"
"${COMPOSE[@]}" ps

echo "==> logs (10 s)"
timeout 10 "${COMPOSE[@]}" logs -f --tail=20 || true

echo "==> health"
sleep 2
"${COMPOSE[@]}" exec -T api python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health', timeout=5).read().decode())"
