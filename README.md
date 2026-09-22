# Montaj Bot

Telegram bot + Mini App that edits users' videos with AI. Design docs: `docs/ARCHITECTURE.md`.

## Quick start

```bash
cp .env.example .env
make up
curl -s localhost:8000/health   # {"status":"ok","db":true,"redis":true}
```

Other targets: `make test`, `make lint`, `make fmt`, `make migrate`, `make revision m="message"`, `make down`.

Optional services use compose profiles: `bot`, `worker`, `tgapi` (e.g. `docker compose --profile worker up -d`).
