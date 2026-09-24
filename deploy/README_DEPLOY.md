# Deploying Montaj Bot

One server runs everything (Postgres, Redis, api, bot, worker, a local Telegram Bot API server, Caddy for HTTPS). Video
files live in Azure Blob Storage. **You do not run Docker commands by hand: `./install.sh` does it.**

## What you need first

1. **A server**: Ubuntu/Debian, 2 vCPU / 4 GB RAM / 15+ GB free disk, ports 80 and 443 open. (Azure `Standard_D2ads_v6` or
   `Standard_B2s` work; `deploy/azure_setup.sh` can create the server and the storage for you, see below.)
2. **A DNS name** whose A record points at the server's public IP (HTTPS certificates are issued automatically).
3. **An Azure Storage account** (Standard LRS is enough). The containers are created by the installer.
4. **A Telegram bot token** from @BotFather (`/newbot`) and `API_ID` / `API_HASH` from https://my.telegram.org
   ("API development tools"): the local Bot API server needs them to send files up to 2 GB.
5. **Keys** for the providers you chose in `.env`: Gemini or Azure OpenAI (`LLM_PROVIDER`), Groq or Azure Speech (`STT_PROVIDER`).

## Install

```bash
git clone <repo-url> montaj-bot && cd montaj-bot      # private repo: use `gh auth login` or an access-token URL
./install.sh          # 1st run: creates .env, GENERATES the secrets, prints what you still have to fill in, exits
nano .env             # fill exactly the values it listed
./install.sh          # 2nd run: everything else
```

What the second run does, in order: validates `.env` (and derives `PUBLIC_BASE_URL`, `DATABASE_URL`, the public blob
endpoint) -> checks the bot token with Telegram -> warns about DNS/RAM/disk problems -> installs Docker + Compose if missing ->
opens ports 80/443 in `ufw` if active -> `git pull` (when the checkout has a remote) -> builds the image -> `alembic upgrade head`
-> creates the blob containers and sets Blob CORS -> starts all services -> installs the nightly database backup (03:15) ->
waits for `https://<domain>/health`.

Flags: `./install.sh --check` validates/prepares `.env` only (no Docker, no network); `--no-backup` skips the backup cron.

### Rules the installer enforces (do not fight them)

- `POSTGRES_PASSWORD` and `PHONE_HASH_PEPPER` are generated once. If a database volume already exists it refuses to invent new
  ones: the pepper is part of the stored phone-number hashes (rotating it breaks the free-trial abuse check) and the password
  protects the existing database.
- `PUBLIC_DOMAIN` must be a bare host name; the Mini App only works over HTTPS.
- The Azurite (local emulator) connection string is rejected for production.

## Update

```bash
cd montaj-bot && ./install.sh          # pulls, rebuilds, migrates BEFORE restarting, restarts, verifies
```

Migrations always run before the new containers start (new code may query new tables). They are forward-only: restore a
backup if one must be undone. Rollback of code: check out the previous commit and run `./install.sh` again.

## Creating the Azure resources (optional helper)

From a machine with the Azure CLI (`az login`):

```bash
STORAGE_ACCOUNT=montajprod123 ./deploy/azure_setup.sh
```

Creates the resource group, the storage account with private containers and a 2-day delete rule, and one VM with ports
22/80/443 open. It prints the VM IP and the `AZURE_STORAGE_CONNECTION_STRING` line for `.env`. Flags were written against the
Azure CLI reference: run `az <command> --help` if one is rejected. The app also deletes expired blobs itself (`cleanup_expired`
cron), so the lifecycle rule is a safety net, not a requirement.

## Verify

```bash
python3 scripts/e2e_smoke.py https://<PUBLIC_DOMAIN>       # HTTPS, health, Mini App, auth rejection
```

Then walk through `docs/QA_CHECKLIST.md` by hand in Telegram (upload, plan, render, delivery, payment). Give yourself units with
`/grant <telegram_id> <units>` from an id listed in `ADMIN_TELEGRAM_IDS`.

## Operations

```bash
C="docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml"
$C ps
$C logs -f worker            # render pipeline; `render stages clips=..s join=..s final=..s` timings per job
$C logs caddy                # certificate problems
$C run --rm api python scripts/cost_report.py --days 7
./deploy/backup.sh           # manual database backup
```

- **Backups**: nightly `pg_dump | gzip` to `/var/backups/montaj` (newest 14 kept) plus a copy in the private blob container
  `AZURE_BACKUP_CONTAINER`, uploaded from inside the api image (no Azure CLI needed). Test a restore at least once:
  `gunzip -c dump.sql.gz | $C exec -T db psql -U montaj <scratch_db>`.
- **Memory**: caps are `*_MEM_LIMIT` in `.env` (defaults in `docker-compose.prod.yml`). A 1080p multi-clip render peaks around
  1 GB of ffmpeg memory; the worker default of 3072m leaves headroom. If a job fails with ffmpeg exit code -9 the container hit its
  memory limit.
- **Each service needs its own restart** after a code update if you bypass the installer: `up -d --force-recreate api bot worker`
  (the api container serves the Mini App and the HTTP routes; forgetting it leaves users on the old version).

## The local Telegram Bot API server

A bot talks to only one Bot API server at a time. If this bot was ever used with the public cloud API (for example during
development), log it out of the cloud BEFORE the first start against the local server:

```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/logOut"      # must return {"ok":true,"result":true}
```

The cloud API then refuses this bot for ~10 minutes; start the local server, then the bot. To go back to the cloud API, call
`/logOut` on the local server the same way (from inside the compose network or the server itself:
`curl "http://localhost:8081/bot<BOT_TOKEN>/logOut"`). Its data lives in the named volume `tgapi-data`.

## Limitations

- Single server = single point of failure; renders share CPU with the API. Scale by a bigger server first.
- Blob lifecycle deletes inputs/outputs after 2 days; the 48 h delivery link fits inside that.
- Manual QA checklist items are not automated.
