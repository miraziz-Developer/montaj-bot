# Mini App: local development notes

The Mini App is plain static files in `miniapp/` (no npm, no bundler), served by the API at `/app/`.

## Run everything locally (Azurite)

```bash
cp .env.example .env            # first time only
# edit .env: BOT_TOKEN=<any string for local dev, e.g. 123456:DEV-TOKEN>
make up                          # db, redis, azurite, api
make migrate
docker compose run --rm api python scripts/configure_storage_cors.py   # containers + CORS (once)
```

`AZURE_PUBLIC_BLOB_ENDPOINT=http://localhost:10000/devstoreaccount1` (already in `.env.example`) makes the
SAS URLs point at Azurite as the BROWSER sees it; the server itself still talks to `azurite:10000` inside Docker.

## Open the page outside Telegram (`?debug=1`)

Real `initData` only exists inside Telegram. For local testing:

1. Create a user that finished onboarding (P05 does this via the bot; until then insert one in Postgres).
2. Produce a signed initData string for that user with your `BOT_TOKEN`
   (see `sign_init_data` in `tests/helpers.py`), then in the browser console on `http://localhost:8000/app/`:
   `localStorage.setItem("__DEV_INIT_DATA__", "<initData>")`
3. Open `http://localhost:8000/app/?debug=1`. The flag only works when the hostname is `localhost`/`127.0.0.1`.

initData expires after 24 h (`max_age_sec`), so regenerate it when the API answers 401.

## Try it inside Telegram (HTTPS is required)

Telegram only opens HTTPS Mini Apps. Expose the local API:

```bash
cloudflared tunnel --url http://localhost:8000     # or: ngrok http 8000
```

Then set `PUBLIC_BASE_URL=https://<tunnel-host>` in `.env`, re-run `configure_storage_cors.py`, and set the Mini App URL
in @BotFather: `/mybots` -> your bot -> Bot Settings -> Menu Button (or Configure Mini App) -> `https://<tunnel-host>/app/`.
Azurite must also be reachable by the phone: with a tunnel for port 10000 set `AZURE_PUBLIC_BLOB_ENDPOINT` to that
tunnel URL plus `/devstoreaccount1`.

## Tests

```bash
node --test "tests/js/*.test.mjs"      # upload logic, Node >= 22, no packages
```
