#!/usr/bin/env bash
# Montaj Bot - one-command server setup (Ubuntu/Debian server; needs root or sudo).
#
#   git clone <repo-url> montaj-bot && cd montaj-bot
#   ./install.sh        # 1st run: creates .env, generates the secrets, lists what YOU must fill in, exits
#   nano .env           # fill the listed values
#   ./install.sh        # 2nd run: installs Docker, builds, migrates, starts everything, checks HTTPS
#
# Re-running is safe and is also how you UPDATE (git pull -> rebuild -> migrate -> restart).
#
# Flags:  --check        only prepare + validate .env (no Docker, no network, no root); exit 0 if ready
#         --no-backup    do not install the nightly database backup cron job
set -euo pipefail
cd "$(dirname "$0")"

CHECK_ONLY=0
WITH_BACKUP=1
say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --no-backup) WITH_BACKUP=0 ;;
    -h | --help) sed -n '2,14p' "$0"; exit 0 ;;
    *) die "unknown flag: $arg (see ./install.sh --help)" ;;
  esac
done

# ---------- .env helpers (never `source .env`: values such as connection strings contain ';') ----------

env_get() { { grep -E "^$1=" .env 2>/dev/null || true; } | head -n1 | cut -d= -f2- | tr -d '\r'; }

env_set() {
  local tmp
  tmp="$(mktemp)"
  K="$1" V="$2" awk 'BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"]; done = 0 }
    index($0, k "=") == 1 { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }' .env >"$tmp"
  cat "$tmp" >.env
  rm -f "$tmp"
}

rand_hex() { openssl rand -hex "${1:-24}" 2>/dev/null || head -c "${1:-24}" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# A database volume that already exists holds data protected by the CURRENT password/pepper: never rotate them.
db_exists() {
  ((CHECK_ONLY)) && return 1 # --check never touches Docker
  command -v docker >/dev/null 2>&1 && docker volume ls -q 2>/dev/null | grep -qE '(^|_)pgdata$'
}

# ---------- 1. .env: create, fill derivable values, validate ----------

FRESH_ENV=0
if [[ ! -f .env ]]; then
  cp .env.example .env
  FRESH_ENV=1
  say "created .env from .env.example"
fi
chmod 600 .env

env_set ENV prod

if [[ -z "$(env_get POSTGRES_PASSWORD)" ]]; then
  db_exists && die "POSTGRES_PASSWORD is empty but a database volume already exists: put its current password in .env"
  env_set POSTGRES_PASSWORD "$(rand_hex 24)"
  say "generated POSTGRES_PASSWORD"
fi
PG_PASSWORD="$(env_get POSTGRES_PASSWORD)"
DB_URL="$(env_get DATABASE_URL)"
if [[ -z "$DB_URL" || "$DB_URL" == *"montaj:montaj@"* ]]; then
  env_set DATABASE_URL "postgresql+asyncpg://montaj:${PG_PASSWORD}@db:5432/montaj"
elif [[ "$DB_URL" != *":${PG_PASSWORD}@db:"* ]]; then
  die "DATABASE_URL does not use POSTGRES_PASSWORD (host must be 'db'): they must match"
fi

PEPPER="$(env_get PHONE_HASH_PEPPER)"
if [[ -z "$PEPPER" || "$PEPPER" == "change-me" ]]; then
  db_exists && die "PHONE_HASH_PEPPER is unset but a database already exists: changing it breaks the free-trial abuse check; restore the original value"
  env_set PHONE_HASH_PEPPER "$(rand_hex 32)"
  say "generated PHONE_HASH_PEPPER (keep it forever: it is part of the phone-number hashes)"
fi

DOMAIN="$(env_get PUBLIC_DOMAIN)"
[[ "$DOMAIN" != http* && "$DOMAIN" != */* ]] || die "PUBLIC_DOMAIN must be a bare host name like bot.example.com (no https://, no path)"
BASE_URL="$(env_get PUBLIC_BASE_URL)"
if [[ -n "$DOMAIN" && ( -z "$BASE_URL" || "$BASE_URL" == http://localhost* ) ]]; then
  env_set PUBLIC_BASE_URL "https://${DOMAIN}"
fi

CONN="$(env_get AZURE_STORAGE_CONNECTION_STRING)"
if [[ -n "$CONN" && "$CONN" != *devstoreaccount1* ]]; then
  PUBLIC_BLOB="$(env_get AZURE_PUBLIC_BLOB_ENDPOINT)"
  if [[ -z "$PUBLIC_BLOB" || "$PUBLIC_BLOB" == http://localhost* ]]; then
    ACCOUNT="$(sed -n 's/.*AccountName=\([^;]*\).*/\1/p' <<<"$CONN")"
    SUFFIX="$(sed -n 's/.*EndpointSuffix=\([^;]*\).*/\1/p' <<<"$CONN")"
    [[ -z "$ACCOUNT" ]] || env_set AZURE_PUBLIC_BLOB_ENDPOINT "https://${ACCOUNT}.blob.${SUFFIX:-core.windows.net}"
  fi
fi

MISSING=()
need() { [[ -n "$(env_get "$1")" ]] || MISSING+=("$1"); }
need BOT_TOKEN
need PUBLIC_DOMAIN
need AZURE_STORAGE_CONNECTION_STRING
need TELEGRAM_API_ID
need TELEGRAM_API_HASH
if [[ "$CONN" == *devstoreaccount1* ]]; then MISSING+=("AZURE_STORAGE_CONNECTION_STRING (still the local Azurite dev value)"); fi
case "$(env_get LLM_PROVIDER)" in
  azure) need AZURE_OPENAI_API_KEY; need AZURE_OPENAI_ENDPOINT; need AZURE_ANALYSIS_DEPLOYMENT; need AZURE_PLANNER_DEPLOYMENT ;;
  *) need GEMINI_API_KEY; need GEMINI_ANALYSIS_MODEL; need GEMINI_PLANNER_MODEL ;;
esac
case "$(env_get STT_PROVIDER)" in
  azure) need AZURE_SPEECH_API_KEY; need AZURE_SPEECH_ENDPOINT ;;
  *) need GROQ_API_KEY ;;
esac

if ((${#MISSING[@]} > 0)); then
  if ((FRESH_ENV)); then
    printf '\n\033[1mThe secrets are generated. Now open .env and fill in the values below, then run ./install.sh again.\033[0m\n'
  else
    printf '\n\033[1;31m.env is not complete yet.\033[0m Fill in these values, then run ./install.sh again:\n'
  fi
  printf '  - %s\n' "${MISSING[@]}"
  cat <<'HINT'

Where to get them:
  BOT_TOKEN                        @BotFather -> /newbot
  TELEGRAM_API_ID / _HASH          https://my.telegram.org -> "API development tools" (needed for files up to 2 GB)
  PUBLIC_DOMAIN                    a DNS name whose A record points at THIS server (HTTPS certificate is automatic)
  AZURE_STORAGE_CONNECTION_STRING  Azure Portal -> Storage account -> Access keys (containers are created for you)
  GEMINI_* / AZURE_OPENAI_*        the LLM you chose in LLM_PROVIDER;  GROQ_API_KEY / AZURE_SPEECH_*: STT_PROVIDER
Also useful: ADMIN_TELEGRAM_IDS (your Telegram id, for admin commands) and PAYMENT_CARD_TEXT (card shown to payers).
HINT
  exit 2
fi

[[ -n "$(env_get ADMIN_TELEGRAM_IDS)" ]] || warn "ADMIN_TELEGRAM_IDS is empty: nobody can use the admin/payment-approval commands"
[[ -n "$(env_get PAYMENT_CARD_TEXT)" ]] || warn "PAYMENT_CARD_TEXT is empty: users will not see where to pay"

if ((CHECK_ONLY)); then
  say ".env is complete and consistent (--check: nothing else was touched)"
  exit 0
fi

# ---------- 2. root, environment sanity ----------

if [[ $EUID -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || die "run this as root (or install sudo)"
  exec sudo -E bash "$0" "$@"
fi
OWNER="${SUDO_USER:-root}"
chown "$OWNER" .env 2>/dev/null || true

MEM_MB="$(awk '/MemTotal/ { print int($2 / 1024) }' /proc/meminfo 2>/dev/null || echo 0)"
DISK_MB="$(df -Pm . | awk 'NR == 2 { print $4 }')"
((MEM_MB >= 3500)) || warn "only ${MEM_MB} MB RAM: renders need about 4 GB (the worker is capped at WORKER_MEM_LIMIT); expect failures on long videos"
((DISK_MB >= 15000)) || warn "only ${DISK_MB} MB free disk: renders and downloads use temporary space (>= 15 GB recommended)"

DOMAIN_IP="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR == 1 { print $1 }')"
MY_IP="$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
if [[ -z "$DOMAIN_IP" ]]; then
  warn "DNS: $DOMAIN does not resolve yet. Create an A record -> ${MY_IP:-this server}; the HTTPS certificate cannot be issued until it does"
elif [[ -n "$MY_IP" && "$DOMAIN_IP" != "$MY_IP" ]]; then
  warn "DNS: $DOMAIN -> $DOMAIN_IP but this server's public IP is $MY_IP; fix the A record or HTTPS will fail"
fi

TOKEN="$(env_get BOT_TOKEN)"
RESP="$(curl -sS --max-time 10 "https://api.telegram.org/bot${TOKEN}/getMe" 2>/dev/null || true)"
if [[ -z "$RESP" ]]; then
  warn "could not reach api.telegram.org to verify BOT_TOKEN (continuing)"
elif [[ "$RESP" != *'"ok":true'* ]]; then
  die "Telegram rejected BOT_TOKEN: create or rotate it in @BotFather"
elif [[ -z "$(env_get BOT_USERNAME)" ]]; then
  BOT_NAME="$(sed -n 's/.*"username":"\([^"]*\)".*/\1/p' <<<"$RESP")"
  [[ -z "$BOT_NAME" ]] || { env_set BOT_USERNAME "$BOT_NAME"; say "BOT_USERNAME=$BOT_NAME"; }
fi

# ---------- 3. Docker ----------

compose_ok() {
  local v
  v="$(docker compose version --short 2>/dev/null | sed 's/^v//')"
  [[ -n "$v" && "$(printf '%s\n2.24.0\n' "$v" | sort -V | head -n1)" == "2.24.0" ]]
}
if ! compose_ok; then
  [[ "$(uname -s)" == Linux ]] || die "install Docker with Compose >= 2.24 first, then re-run"
  say "installing Docker Engine + Compose (official get.docker.com script)"
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sh /tmp/get-docker.sh
  systemctl enable --now docker 2>/dev/null || true
  compose_ok || die "Docker Compose >= 2.24 is required but could not be installed"
fi
say "Docker $(docker --version | cut -d' ' -f3 | tr -d ,) / Compose $(docker compose version --short)"

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  say "opening ports 80/443 in ufw"
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw allow 443/udp >/dev/null
fi

# ---------- 4. update code (if this is a git checkout with a remote), then deploy ----------

if [[ -d .git ]] && git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
  say "git pull"
  sudo -u "$OWNER" git pull --ff-only || warn "git pull failed: deploying the code that is already here"
fi
SKIP_PULL=1 ./deploy/deploy.sh

# ---------- 5. nightly backup ----------

if ((WITH_BACKUP)); then
  mkdir -p /var/backups/montaj
  chmod 700 /var/backups/montaj
  JOB="15 3 * * * cd $(pwd) && ./deploy/backup.sh >> /var/log/montaj-backup.log 2>&1 # montaj-backup"
  { crontab -l 2>/dev/null | grep -v '# montaj-backup' || true; echo "$JOB"; } | crontab -
  say "nightly database backup installed (03:15, 14 local copies + Azure Blob '$(env_get AZURE_BACKUP_CONTAINER)')"
fi

# ---------- 6. verify from the outside ----------

say "waiting for https://${DOMAIN}/health (the first start issues the certificate: up to ~2 minutes)"
OK=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 5 "https://${DOMAIN}/health" 2>/dev/null | grep -q '"status":"ok"'; then OK=1; break; fi
  sleep 5
done

echo
if ((OK)); then
  printf '\033[1;32mMontaj Bot is running.\033[0m\n  Mini App / API : https://%s/app/\n  Health         : https://%s/health\n' "$DOMAIN" "$DOMAIN"
  printf '  Bot            : open @%s in Telegram and send /start\n' "$(env_get BOT_USERNAME)"
else
  printf '\033[1;33mContainers are up, but https://%s/health did not answer yet.\033[0m\n' "$DOMAIN"
  echo "  Usually DNS is not pointing here yet or the certificate is still being issued."
  echo "  Check: docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml logs caddy"
fi
cat <<'TIPS'

Useful commands (run in this directory):
  ./install.sh                                   update to the latest code and restart (safe to re-run)
  docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml logs -f worker
  docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml ps
TIPS
[[ "$OWNER" == root ]] || chown "$OWNER" .env 2>/dev/null || true
