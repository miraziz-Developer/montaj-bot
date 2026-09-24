# Montaj Bot

A Telegram bot + Mini App that edits people's videos with AI. A blogger uploads a video, the AI watches it and proposes an
edit plan in plain Uzbek, the user approves or asks for changes in chat, and a worker renders the finished video with FFmpeg
and sends it back.

**The AI plans, FFmpeg executes.** The LLM never touches the video: it produces a validated JSON `EditPlan`, and a
deterministic renderer turns that plan into FFmpeg commands.

## What it does

- Upload from the Mini App straight to Azure Blob (chunked, resumable, up to 3 GB), server-side ffprobe validation.
- Speech-to-text (Groq Whisper or Azure AI Speech), scene detection, AI analysis (Gemini or Azure OpenAI), AI plan.
- Professional rendering: word-highlight captions, silence/filler cutting on word boundaries, punch-in zoom rhythm, slow
  Ken Burns push-in, clean cross-dissolves at real cuts, background music with ducking, loudness normalisation.
- Optional **B-roll**: attach up to 4 extra clips; the AI cuts to them while the main narration keeps playing.
- Robust with real-world footage: rotated phone video, HDR (HLG/PQ tone-mapped to SDR), non-square pixels, round Telegram
  video notes, variable frame rate, missing/short audio tracks, any container/codec ffmpeg can read.
- Units-based billing with manual card payments approved by an admin in Telegram, free trial, referrals, cost tracking.

## Install on a server (one command)

Needs an Ubuntu/Debian server (4 GB RAM, 15 GB free disk), a DNS name pointing at it, and an Azure Storage account.
See [deploy/README_DEPLOY.md](deploy/README_DEPLOY.md) for creating those.

```bash
git clone <this-repo-url> montaj-bot && cd montaj-bot
./install.sh          # 1st run: creates .env, generates the secrets, lists what you must fill in
nano .env             # fill the listed values (bot token, domain, Azure connection string, LLM/STT keys)
./install.sh          # 2nd run: installs Docker, builds, migrates, starts everything, checks HTTPS
```

`install.sh` does everything else: installs Docker, derives `PUBLIC_BASE_URL`/`DATABASE_URL`/blob endpoint, verifies the bot
token, checks DNS/RAM/disk, builds one image, runs migrations, creates the blob containers and CORS, starts api + bot + worker
+ Postgres + Redis + local Telegram Bot API + Caddy (automatic HTTPS), installs a nightly database backup, and waits until
`https://<domain>/health` answers. Re-running it is safe and is also how you **update**.

## Local development

```bash
cp .env.example .env      # defaults point at Azurite (local storage emulator) and local Postgres/Redis
make up                   # db, redis, azurite, api (http://localhost:8000/health)
docker compose --profile worker --profile bot --profile tgapi up -d   # optional: worker, bot, local Bot API
make test                 # pytest inside Docker (real Postgres, real ffmpeg on synthetic video)
make lint                 # ruff check;  make fmt formats
make test-js              # Mini App upload logic (node --test)
make test-install         # install.sh behaviour (no Docker needed)
```

Other targets: `make migrate`, `make revision m="message"`, `make logs`, `make down`.
Mini App without Telegram: `http://localhost:8000/app/?debug=1` (see `scripts/dev_mini_app_notes.md`).
Analyse a local video without Telegram/DB: `docker compose run --rm api python scripts/dev_analyze.py /app/video.mp4`.

## Configuration

Everything is an environment variable, listed with defaults in [.env.example](.env.example) and typed in
`app/core/config.py`. The ones you choose between:

| Variable | Options |
|---|---|
| `LLM_PROVIDER` | `gemini` (native video input) or `azure` (Azure OpenAI, one frame per scene) |
| `STT_PROVIDER` | `groq` (Whisper) or `azure` (Azure AI Speech, `uz-UZ,ru-RU`) |
| `RENDER_STABILIZE` | `false` (default) / `true`: ffmpeg `deshake`, ~2x slower clips |
| `WORKER_MEM_LIMIT` | default 3072m; a 1080p render peaks around 1 GB of ffmpeg memory |

## Repository layout

```
app/            FastAPI api, aiogram bot, arq worker, services (media, stt, ai, render), SQLAlchemy models
miniapp/        the Mini App: plain HTML/CSS/JS (no framework, no bundler)
migrations/     Alembic
assets/         fonts (OFL) and royalty-free music (CC0), see assets/music/LICENSE.md
deploy/         production compose, Caddyfile, deploy/backup scripts, Azure resource setup
scripts/        cost report, e2e smoke test, storage CORS, dev helpers
docs/           ARCHITECTURE.md, EDIT_PLAN_SCHEMA.md, RUNTIME_PROMPTS.md, QA_CHECKLIST.md
tests/          unit, integration (real Postgres + ffmpeg), js, shell
install.sh      one-command server setup
```

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, data model, job state machine, pipeline, HTTP API (source of truth)
- [docs/EDIT_PLAN_SCHEMA.md](docs/EDIT_PLAN_SCHEMA.md) — the EditPlan contract, post-processing rules, render recipe
- [docs/RUNTIME_PROMPTS.md](docs/RUNTIME_PROMPTS.md) — the prompts the product sends to the LLM
- [docs/QA_CHECKLIST.md](docs/QA_CHECKLIST.md) — manual checks before real users
- [deploy/README_DEPLOY.md](deploy/README_DEPLOY.md) — server setup, operations, backups
- [AGENTS.md](AGENTS.md) — rules for coding agents working on this repo

## Operations cheat sheet

```bash
C="docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml"
$C ps                                   # what is running
$C logs -f worker                       # follow the render pipeline (also prints `render stages ...` timings)
$C run --rm api python scripts/cost_report.py --days 7
./deploy/backup.sh                      # manual database backup (nightly one is installed by install.sh)
```

## Known limitations

- One VM = one point of failure; renders share CPU with the API. Scale by a bigger VM first.
- Render speed: roughly 2-3 minutes for a ~1 minute 1080p output on 2 vCPU.
- Output sharpness is bounded by the source: a 400 px round video note upscaled to 1080p stays soft.
- An attached B-roll clip cannot be removed again in the Mini App (cancel the job and start over).
- Uzbek speech recognition is imperfect: review the plan, captions follow the transcript.
