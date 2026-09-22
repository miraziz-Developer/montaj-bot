# Deploying Montaj Bot to one Azure VM

One VM runs everything (Postgres, Redis, api, bot, worker, local Telegram Bot API, Caddy for HTTPS). Files
live in Azure Blob. Nothing here was run against a real Azure subscription: commands marked `# VERIFY` in
the scripts should be checked with `az <cmd> --help` on first use.

## 0. Prerequisites
- Azure CLI logged in (`az login`), a domain you control, a bot token from @BotFather (rotate it if it was ever pasted in chat).
- `API_ID` / `API_HASH` from https://my.telegram.org (see `telegram-bot-api.env.example`) for files > 50 MB.
- Real `GEMINI_API_KEY` + `GEMINI_*_MODEL` names, `GROQ_API_KEY`.
- Fonts (Montserrat, NotoSans, Inter; OFL) in `assets/fonts/` before building the image.

## 1. Create the Azure resources
```bash
STORAGE_ACCOUNT=montajprod123 ./deploy/azure_setup.sh
```
Creates the resource group, private containers (`uploads`, `artifacts`, `outputs`, `backups`), a 2-day delete rule
and the VM (ports 22/80/443). It prints the VM IP and the `AZURE_STORAGE_CONNECTION_STRING` line for `.env`.
Point a DNS **A record** for your domain at the VM IP.

## 2. Configure Blob CORS (needed by the Mini App direct upload)
Run `scripts/configure_storage_cors.py` with the production `PUBLIC_BASE_URL` origin allowed.

## 3. Put the code on the VM
The project is not a git repository yet: `git init`, commit and push to a PRIVATE remote, then on the VM
`git clone <remote> ~/montaj-bot`. (Or `rsync -a --exclude .env --exclude .git ./ azureuser@IP:~/montaj-bot/`.)

## 4. `.env` on the VM
`cp .env.example .env`, then fill: `BOT_TOKEN`, `PUBLIC_DOMAIN`, `PUBLIC_BASE_URL=https://<PUBLIC_DOMAIN>`,
`POSTGRES_PASSWORD`, `DATABASE_URL` (same password, host `db`), `AZURE_STORAGE_CONNECTION_STRING`,
`AZURE_PUBLIC_BLOB_ENDPOINT` (the account's `https://<acct>.blob.core.windows.net`), `GEMINI_*`, `GROQ_API_KEY`,
`TELEGRAM_API_ID/HASH`, `ADMIN_TELEGRAM_IDS`, `PAYMENT_CARD_TEXT`. Never commit `.env`.

## 5. Deploy
```bash
./deploy/deploy.sh          # or DEPLOY_HOST=azureuser@IP ./deploy/deploy.sh
```
It validates `.env`, builds, runs `alembic upgrade head`, starts everything and prints health.

## 6. Verify
```bash
python3 scripts/e2e_smoke.py https://<PUBLIC_DOMAIN>
```
Then walk through `docs/QA_CHECKLIST.md` by hand in Telegram (upload, plan, render, delivery, payment).

## 7. Backups (nightly)
```bash
sudo mkdir -p /var/backups/montaj && sudo chown $USER /var/backups/montaj
( crontab -l 2>/dev/null; echo '15 3 * * * cd ~/montaj-bot && ./deploy/backup.sh >> ~/montaj-backup.log 2>&1' ) | crontab -
```
Keeps 14 local dumps and uploads each to the private `backups` container. Test a restore at least once:
`gunzip -c dump.sql.gz | docker compose ... exec -T db psql -U montaj <scratch_db>`.

## 8. Operations
- Logs: `docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml logs -f worker`
- Costs: `docker compose ... run --rm api python scripts/cost_report.py --days 7`
- Update: `./deploy/deploy.sh`. Rollback: check out the previous commit and run it again (migrations are forward-only: restore a backup if one must be undone).

## Limitations
- Single VM = single point of failure; renders share CPU with the API. Scale by a bigger VM first.
- Blob lifecycle deletes inputs/outputs after 2 days; the 48 h delivery link fits inside that.
- Manual QA checklist items are not automated.
