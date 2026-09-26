# Montaj Bot — Architecture (source of truth)

## 1. Goal and non-goals
**Goal:** A Telegram bot + Mini App where a blogger uploads ANY video (any niche), an AI watches it, proposes a
clear edit script, the user can ask for changes in plain text, and after approval the system renders a professional
edit fast and cheaply.

**Key principle: the AI PLANS, FFmpeg EXECUTES.** The LLM never touches video. It outputs a validated JSON `EditPlan`
(see docs/EDIT_PLAN_SCHEMA.md). A deterministic renderer turns the plan into FFmpeg commands.

**Non-goals** (do NOT build now): stock b-roll libraries, AI-generated footage, face tracking, voice-message feedback,
payment-provider integration (manual approval first), multi-language UI (Uzbek only), web admin panel.
(User-attached B-roll clips, the slow Ken Burns push-in and crossfade transitions ARE implemented: see 10 and EDIT_PLAN_SCHEMA.md.)

## 2. Users and main flows
1. `/start` -> welcome -> ask niche -> ask purpose -> (optional) share phone for free trial -> main menu.
2. "Video yuklash" button opens the Mini App -> user picks a file -> chunked upload straight to Azure Blob ->
   server verifies with ffprobe -> Mini App offers OPTIONAL B-roll (up to `MAX_BROLL_SOURCES_PER_JOB` extra clips, each
   uploaded the same way and attached to the job) -> shows "N birlik, davom etamizmi?" + choose format & style -> confirm.
3. Bot posts progress in ONE edited message: prepare -> transcribe -> analyze scenes -> build plan.
4. Bot sends the plan (Uzbek text, timeline) with buttons: ✅ Tasdiqlash / ✏️ O‘zgartirish / ❌ Bekor qilish.
5. "O‘zgartirish": user types what to change -> new plan version -> back to step 4 (2 free revisions per job).
6. ✅ -> render -> deliver video in chat (<= 1.9 GB) or a 48h download link (bigger, or on send failure).

## 3. Components
```
Telegram user
   |  chat                         |  Mini App (HTML/JS, served by FastAPI at /app)
   v                               v
[aiogram bot process]        [FastAPI api process] -- initData auth -- Postgres
   |  (long polling via           |  SAS URL                           Redis (arq queue, FSM)
   |   local Bot API server)      v
   |                        [Azure Blob: uploads | artifacts | outputs]
   |                              ^
   +------ enqueue jobs -----> [arq worker process] -- ffmpeg, PySceneDetect
                                   |-- STT provider (Groq Whisper or Azure AI Speech: STT_PROVIDER)
                                   |-- LLM (Gemini or Azure OpenAI: LLM_PROVIDER; video analysis + planner)
                                   +-- sends Telegram messages/files via local Bot API server
```
Processes (all from the same repo/image): `api`, `bot`, `worker`. Plus containers: `db`, `redis`, `telegram-bot-api`
(local Bot API server, needed to SEND files up to 2 GB), `azurite` (dev only).

## 4. Tech decisions (fixed)
- Bot updates: **long polling** (no webhook) against the local Bot API server. FSM storage: Redis.
- Mini App auth: header `Authorization: tma <initData>` verified on every request (no JWT, no sessions).
- Uploads: browser -> Azure Blob directly, Block Blob API (`Put Block`, `Put Block List`) with a write-only SAS URL.
- Queue: arq (Redis). `job_timeout` MUST be raised (default 300 s is far too small). One worker process,
  `WORKER_CONCURRENCY=1` by default (Azure quota is small).
- Bot does NOT download videos from chat in MVP. If a user sends a video in chat, reply "use the Mini App button".
- Storage access in MVP uses the storage account connection string (account key). Managed identity is a later upgrade.
- LLM and STT sit behind interfaces (`LLMClient`, `STTProvider`) so providers can be swapped. Implemented: LLM =
  Gemini (native video input) or Azure OpenAI (`azure_llm.py`: one extracted frame per scene, strict JSON-schema output);
  STT = Groq Whisper or Azure AI Speech Fast Transcription (`azure_speech.py`, `uz-UZ,ru-RU` auto-detect).

## 5. Repository layout
```
montaj-bot/
  AGENTS.md README.md install.sh            (install.sh: one-command server setup, see deploy/README_DEPLOY.md)
  docs/ (ARCHITECTURE.md, EDIT_PLAN_SCHEMA.md, RUNTIME_PROMPTS.md, QA_CHECKLIST.md)
  app/
    core/        config.py logging.py db.py security.py errors.py
    models/      user.py ledger.py upload.py job.py job_source.py plan.py payment.py enums.py
    schemas/     edit_plan.py analysis.py transcript.py api.py
    api/         main.py deps.py routers/{me,uploads,jobs,health}.py
    bot/         main.py texts.py keyboards.py states.py handlers/{start,menu,plans,admin,billing}.py notifier.py
    services/
      users.py billing.py tariffs.py units.py storage.py jobs.py
      media/     ffmpeg.py probe.py proxy.py audio.py silence.py scenes.py
      stt/       base.py groq_whisper.py azure_speech.py fake.py
      ai/        llm.py azure_llm.py analysis.py planner.py presets.py snap.py plan_validator.py fallback_planner.py
                 plan_text.py prompts/{analysis_system.md,azure_analysis_system.md,planner_system.md,revision_system.md}
      render/    engine.py clip_stage.py concat_stage.py final_stage.py captions_ass.py music.py
    worker/      main.py tasks.py queue.py artifacts.py failures.py recovery.py notifier.py deps.py costs.py
  assets/        fonts/ (Montserrat-Bold.ttf, NotoSans-Bold.ttf, Inter-Bold.ttf)  music/ (catalog.json + mp3s)
  miniapp/       index.html app.js style.css i18n.js
  migrations/    (alembic)
  scripts/       configure_storage_cors.py upload_backup.py cost_report.py e2e_smoke.py dev_analyze.py
  deploy/        docker-compose.prod.yml Caddyfile deploy.sh backup.sh azure_setup.sh lifecycle.json initdb/ README_DEPLOY.md
  tests/         fakes/ unit/ integration/ js/ shell/
  docker-compose.yml Dockerfile Makefile pyproject.toml .env.example
```

## 6. Data model (PostgreSQL, SQLAlchemy 2 typed models, UUID primary keys)
**users**: id, telegram_id (bigint, unique), username, first_name, phone_hash (nullable, unique when not null),
niche (text), purpose (text), language (default 'uz'), onboarding_completed (bool), trial_used (bool),
balance_units (int, default 0, >= 0), referral_code (unique), referred_by (fk users, nullable), is_admin (bool),
created_at.
**unit_ledger** (append-only): id, user_id, delta (int, non-zero), reason (enum: purchase, reserve, refund,
revision, referral, admin_grant), job_id (nullable), payment_id (nullable), note, created_at.
Invariant: `users.balance_units == SUM(unit_ledger.delta)` for that user. Trial jobs write NO ledger row; they only set
`users.trial_used = true` and `jobs.is_trial = true`.
**uploads**: id, user_id, blob_path, original_filename, content_type, size_bytes, status (INIT, UPLOADED, VERIFIED, REJECTED,
EXPIRED), duration_sec, width, height, fps, has_audio, video_codec, created_at, verified_at.
**jobs**: id, user_id, upload_id, status (see 7), is_trial, aspect, style_preset, brief (text), units_cost, revision_count,
current_plan_version, status_message_id (bigint, Telegram message to edit), chat_id, output_blob_path, output_size_bytes,
error_code, error_message, created_at, updated_at, finished_at,
cost tracking: stt_seconds, llm_input_tokens, llm_output_tokens, render_seconds, est_cost_usd (numeric).
**job_sources**: id, job_id, upload_id, role (enum: PRIMARY, BROLL), position (int), created_at; unique (job_id, upload_id).
`jobs.upload_id` is ALWAYS the PRIMARY source (it drives transcript, captions, billing); a PRIMARY row is written when the
job is created. BROLL rows are extra, muted cutaway clips attached while the job is AWAITING_CONFIRM (at most
`MAX_BROLL_SOURCES_PER_JOB`); each adds `BROLL_SURCHARGE_UNITS` to `jobs.units_cost`. Clip `source_id` "broll_N" = the N-th BROLL row by position.
**edit_plans**: id, job_id, version (int, unique with job_id), plan_json (JSONB), human_summary (text),
source (enum: ai_initial, ai_revision, fallback), user_feedback (text nullable), created_at.
**payments**: id, user_id, plan_code, amount_uzs, provider (manual|payme|click), status (pending, approved, rejected),
external_id, receipt_file_id (Telegram file_id of the receipt photo), note (text, nullable: rejection/cancel reason),
decided_by (fk users), created_at, decided_at.
Artifacts (proxy, audio, transcript.json, scenes.json, silences.json, analysis.json, plan_vN.json) live in Blob,
NOT in the DB. Blob paths are derived from job_id (see 9), no extra table.

## 7. Job state machine
```
CREATED --(upload complete + verified)--> AWAITING_CONFIRM
AWAITING_CONFIRM --(confirm: units reserved or trial used)--> QUEUED
QUEUED --(worker starts)--> PREPROCESSING --> ANALYZING --> PLANNING --> AWAITING_PLAN_APPROVAL
AWAITING_PLAN_APPROVAL --(revise)--> REVISING --> AWAITING_PLAN_APPROVAL
AWAITING_PLAN_APPROVAL --(approve)--> RENDERING --> DELIVERING --> DONE
any active state --(unrecoverable error)--> FAILED
AWAITING_CONFIRM | QUEUED | AWAITING_PLAN_APPROVAL --(user cancel)--> CANCELED
AWAITING_PLAN_APPROVAL --(24h without action)--> EXPIRED
```
Rules: transitions go through ONE function `jobs.transition(job_id, from_states, to_state)` implemented as an atomic
`UPDATE ... WHERE id=:id AND status = ANY(:from_states)`; rowcount 0 means "someone else moved it" -> abort quietly.
**Refund policy:** FAILED -> refund all reserved units. CANCELED while QUEUED -> refund. CANCELED/EXPIRED after analysis
started -> no refund (compute already spent). Trial job FAILED -> `trial_used=false` again.

## 8. Units and billing
- `units = max(1, ceil(duration_sec / UNIT_SECONDS))`, `UNIT_SECONDS = 180`. So 1 unit = up to 3 minutes of SOURCE video.
- Units are RESERVED at confirm (ledger `reserve`, negative delta). They are not returned unless the refund policy says so.
- Free revisions: `FREE_REVISIONS_PER_JOB = 2`. The 3rd and later revisions cost `REVISION_COST_UNITS = 1` (ledger `revision`).
- Trial: once per user AND once per phone hash; requires a verified phone (`trial_available = not trial_used and phone_hash is not None`).
  Max source duration `TRIAL_MAX_DURATION_SEC = 60`. Output gets a watermark.
- Tariffs (config in `services/tariffs.py`, prices in UZS): trial 1 video free; single 1 unit 15 000; start 10 units 99 000;
  pro 30 units 249 000 (recommended); max 100 units 690 000. Units do not expire in MVP.
- Payment MVP: manual (user pays by card, sends receipt photo, admin approves in Telegram). Payme/Click later.
- Referral: inviter gets +3 units after the invited user's FIRST job reaches DONE (anti-fraud).

## 9. Storage layout (Azure Blob)
Containers: `uploads`, `artifacts`, `outputs` (all private).
- `uploads/{user_id}/{upload_id}/source{ext}`
- `artifacts/{job_id}/proxy.mp4`, `audio_{n:03d}.ogg`, `transcript.json`, `scenes.json`, `silences.json`,
  `analysis.json`, `broll_sources.json` (per-scene descriptions of the attached B-roll), `plan_v{n}.json`
- `outputs/{job_id}/final.mp4`
Retention: everything deleted after 48 h (Blob lifecycle rule, `daysAfterCreationGreaterThan: 2`) AND by the worker cron
`cleanup_expired` as a safety net. Tell users this in the bot ("fayllar 48 soatdan keyin o‘chiriladi").

## 10. Pipeline (worker)
Working dir `TMP_DIR/{job_id}`, removed in `finally`.
1. **download** source from Blob.
2. **probe** (ffprobe) -> verify again. `probe()` returns DISPLAY geometry: phone videos are stored landscape with a
   rotation flag (width/height are swapped for 90/270), non-square pixels (SAR) are folded into the width, and PQ/HLG
   transfer is flagged `is_hdr`. Every consumer (upload record, planner, output sizing, renderer) relies on this.
3. **proxy**: 720p (480p if source > `LONG_VIDEO_THRESHOLD_SEC`), h264, keyframe every ~2 s, aac 64k. Upload to artifacts.
4. **audio**: extract mono 16 kHz Opus/OGG chunks of <= 600 s -> STT -> `transcript.json` (words + segments, absolute seconds).
5. **silences**: ffmpeg `silencedetect` -> `silences.json`.
   **4b. transcript correction** (`ai/transcript_fix.py`, `TRANSCRIPT_CORRECTION`, Gemini only): per <= 600 s window
   Gemini hears the audio with the STT draft and returns the corrected text; `difflib` aligns it word by word onto the
   STT's word times (misheard words take the old words' time slot). A window whose text differs too much, or a long
   dropped/added run, keeps the STT words. The result overwrites `transcript.json` with `corrected: true` (done once).
6. **scenes**: PySceneDetect on the proxy, then post-process (merge < 1 s, split > 12 s at sentence/silence boundaries,
   or every 8 s if none) -> `scenes.json`.
7. **analysis**: Gemini watches the proxy video (default 1 frame/second + audio) in windows of <= `ANALYSIS_CHUNK_SEC`
   and returns per-scene structured JSON -> `analysis.json` (docs/RUNTIME_PROMPTS.md section A).
8. **plan**: planner LLM (docs/RUNTIME_PROMPTS.md section B) -> validate -> snap cuts to word/silence boundaries ->
   validate against the source -> save `edit_plans` v1. If the LLM fails twice: deterministic fallback planner.
   With B-roll attached: each BROLL upload first gets a proxy, scene detection and a frame-only analysis (NO STT: it is
   muted cutaway footage; cached as `broll_sources.json`); the planner may cut to it with `source_id` "broll_N" while the
   primary narration keeps playing (`audio.source = "primary"`). Validation is per source (bounds, overlaps).
9. Notify the user with the plan. Wait for the user.
10. **revision** (optional loop): docs/RUNTIME_PROMPTS.md section C.
11. **render**: docs/EDIT_PLAN_SCHEMA.md "Render recipe" -> `final.mp4` -> upload to `outputs`.
12. **deliver**: <= 1.9 GB: `send_video` (fallback `send_document`) through the local Bot API; else or on failure: 48 h read-SAS link.
13. Record costs, set DONE, cleanup temp.
Light mode for long videos (> `LONG_VIDEO_THRESHOLD_SEC`, default 1200 s): lower analysis fps (`GEMINI_ANALYSIS_FPS_LONG`),
720p -> 480p proxy, `RENDER_PRESET=ultrafast`, output max 1080p.
Render safety properties (each is covered by tests, see EDIT_PLAN_SCHEMA.md section 6): transitions join in groups of at
most 5 inputs so ffmpeg memory stays around 1 GB for any clip count (a flat graph peaked ~3.6 GB at 15 clips); a hard cut and
a crossfade can share one graph (all video inputs get the same timebase first); HDR is tone-mapped to SDR BT.709;
round Telegram video notes are cropped to their inscribed square. Per-stage timings are logged (`render stages ...`).
Performance expectation on a 2 vCPU VM: analysis 1–4 min for typical videos; a 58 s 9:16 1080p output from a 1080p source
renders in roughly 2-3 min. `deshake` stabilisation is off by default (`RENDER_STABILIZE`): it doubled clip time.

## 11. HTTP API (FastAPI). All `/api/*` need `Authorization: tma <initData>`.
Error body: `{"error": {"code": "...", "message_uz": "..."}}`.
- `GET /health` -> `{"status":"ok","db":true,"redis":true}` (no auth)
- `GET /api/me` -> `{telegram_id, first_name, balance_units, trial_available, onboarding_completed, tariffs:[{code,units,price_uzs,label}]}`
- `POST /api/uploads/init` body `{filename, size_bytes, content_type}` -> `{upload_id, upload_url, block_size, max_parallel, expires_at}`
  errors: FILE_TOO_LARGE 413, UNSUPPORTED_TYPE 415, NO_CREDITS 402, TOO_MANY_ACTIVE_JOBS 429, ONBOARDING_REQUIRED 403
- `POST /api/uploads/{id}/resume` -> same shape as init (fresh SAS), only while status INIT
- `GET /api/uploads/{id}/blocks` -> `{uploaded_block_ids:[...], block_size}` (uncommitted blocks, via server credentials)
- `POST /api/uploads/{id}/complete` -> `{job_id, duration_sec, width, height, units_cost, is_trial, balance_units, balance_after}`
  errors: BLOB_MISSING, SIZE_MISMATCH, INVALID_MEDIA, TOO_LONG, TRIAL_TOO_LONG
- `POST /api/uploads/{id}/attach` body `{job_id}` -> `{upload_id, duration_sec, width, height, broll_count, units_cost}`:
  attaches an already-uploaded video to the caller's job as B-roll (job must be AWAITING_CONFIRM). Same checks as `complete`
  (size, ffprobe, `MAX_VIDEO_DURATION_SEC`); errors: TOO_MANY_BROLL_SOURCES 429, INVALID_STATE 409, NOT_FOUND 404 (+ the `complete` errors)
- `POST /api/jobs/{id}/confirm` body `{aspect, style_preset, brief}` -> `{job_id, status:"QUEUED", balance_units}`
  errors: INSUFFICIENT_UNITS 402, INVALID_STATE 409
- `GET /api/jobs/{id}`, `GET /api/jobs?limit=20`, `POST /api/jobs/{id}/cancel`
- Static Mini App: `GET /app/` (files from `miniapp/`).
Upload details: `block_size = 8 MiB`, `max_parallel = 4`; block id = base64 of zero-padded 6-digit index; all ids equal length.
SAS permissions: create + write only, expiry `UPLOAD_SAS_TTL_MIN`. Storage CORS must allow the Mini App origin (PUT, GET, HEAD, OPTIONS).

## 12. Bot commands and callbacks
Commands: `/start [ref_CODE]`, `/help`, `/balance`, `/tariflar`, `/myvideos`, `/cancel`. Admin only: `/grant <telegram_id> <units>`,
`/stats`. Callback data formats (<= 64 bytes): `onb:niche:<key>`, `onb:purpose:<key>`, `plan:approve:<job_id>`,
`plan:revise:<job_id>`, `plan:cancel:<job_id>`, `buy:<plan_code>`, `pay:ok:<payment_id>`, `pay:no:<payment_id>`.
FSM states: `Onboarding.niche_text`, `Onboarding.purpose_text`, `Onboarding.contact`, `Revision.waiting_text`, `Billing.waiting_receipt`.

## 13. Configuration (env vars; all must be in `.env.example`)
```
ENV=dev
DATABASE_URL=postgresql+asyncpg://montaj:montaj@db:5432/montaj
TEST_DATABASE_URL=postgresql+asyncpg://montaj:montaj@db:5432/montaj_test
REDIS_URL=redis://redis:6379/0
BOT_TOKEN=
BOT_USERNAME=
TELEGRAM_API_BASE=http://telegram-bot-api:8081
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
ADMIN_TELEGRAM_IDS=
PUBLIC_BASE_URL=http://localhost:8000
# production only (deploy/): PUBLIC_DOMAIN, POSTGRES_PASSWORD, AZURE_BACKUP_CONTAINER, *_MEM_LIMIT
# (./install.sh sets ENV=prod, PUBLIC_BASE_URL, DATABASE_URL, AZURE_PUBLIC_BLOB_ENDPOINT and generates the secrets)
PHONE_HASH_PEPPER=change-me          # random, set once, NEVER rotate on a live database (part of the phone hashes)
AZURE_STORAGE_CONNECTION_STRING=
AZURE_UPLOADS_CONTAINER=uploads
AZURE_ARTIFACTS_CONTAINER=artifacts
AZURE_OUTPUTS_CONTAINER=outputs
UPLOAD_SAS_TTL_MIN=120
DOWNLOAD_SAS_TTL_HOURS=48
MAX_UPLOAD_BYTES=3221225472
MAX_VIDEO_DURATION_SEC=3600
TRIAL_MAX_DURATION_SEC=60
UNIT_SECONDS=180
FREE_REVISIONS_PER_JOB=2
REVISION_COST_UNITS=1
MAX_ACTIVE_JOBS_PER_USER=2
MAX_BROLL_SOURCES_PER_JOB=4
BROLL_SURCHARGE_UNITS=1
GEMINI_API_KEY=
GEMINI_ANALYSIS_MODEL=
GEMINI_PLANNER_MODEL=
GEMINI_ANALYSIS_FPS=1
GEMINI_ANALYSIS_FPS_LONG=0.5
GEMINI_MEDIA_RESOLUTION=low
LLM_PROVIDER=gemini                 # or azure: then AZURE_OPENAI_API_KEY / _ENDPOINT / AZURE_ANALYSIS_DEPLOYMENT / AZURE_PLANNER_DEPLOYMENT
LLM_MAX_CONCURRENCY=2
ANALYSIS_CHUNK_SEC=600
LONG_VIDEO_THRESHOLD_SEC=1200
STT_PROVIDER=groq
GROQ_API_KEY=
GROQ_STT_MODEL=whisper-large-v3-turbo
                                    # STT_PROVIDER=azure needs AZURE_SPEECH_API_KEY, AZURE_SPEECH_ENDPOINT, AZURE_SPEECH_LOCALES=uz-UZ,ru-RU
STT_CONCURRENCY=3
TRANSCRIPT_CORRECTION=true         # Gemini fixes the STT words, the STT keeps the word timing (Uzbek accuracy)
WORKER_CONCURRENCY=1
JOB_TIMEOUT_SEC=14400
RENDER_PRESET=veryfast
RENDER_CRF=21
RENDER_CLIP_CONCURRENCY=2
RENDER_STABILIZE=false              # ffmpeg deshake: ~2x slower clips, can fight deliberate pans
RENDER_FACE_TRACKING=true          # virtual camera operator for `fill` reframes; falls back to the AI's static focus
DELIVER_MAX_INLINE_BYTES=1900000000
TMP_DIR=/tmp/montaj
ASSETS_DIR=/app/assets
PRICE_GEMINI_IN_PER_M_USD=0.30
PRICE_GEMINI_OUT_PER_M_USD=2.50
PRICE_STT_PER_HOUR_USD=0.04
PRICE_VM_PER_HOUR_USD=0.10
PAYMENT_CARD_TEXT=
```
(`GEMINI_*_MODEL` and the PRICE_* values are placeholders: the owner sets current model names and prices.)

## 14. Cost tracking (needed to know real margin)
Worker records per job: `stt_seconds`, `llm_input_tokens`, `llm_output_tokens`, `render_seconds`, then
`est_cost_usd = tokens*price + stt_hours*price + render_seconds/3600*VM price`. `scripts/cost_report.py` prints cost per
job and per source-minute, and margin vs tariff revenue per unit.

## 15. Security and privacy summary
initData HMAC on every API call · write-only short-lived SAS · ffprobe validation · text sanitization for ASS ·
per-user active job limit · files auto-deleted after 48 h · no secrets in logs · trial abuse limited by phone hash.
User videos are private: never used for anything except producing the user's result.

## 16. Failure handling
- Every worker stage wrapped: on exception -> log with job_id, `jobs.transition(... FAILED)`, refund per policy,
  friendly Uzbek message to the user, notify admins with the error code.
- Worker startup: jobs stuck in PREPROCESSING/ANALYZING/PLANNING/REVISING/RENDERING/DELIVERING for > 30 min are re-enqueued once;
  second time -> FAILED + refund.
- External calls: timeouts + exponential backoff (max 3 tries) on 429/5xx.
- Telegram send failures: retry 3 times; then fall back to a download link.

## 17. Roadmap (after MVP, NOT now)
Payme/Click · voice-message feedback · in-chat small file upload · face-tracking reframe · stock b-roll library ·
detach/remove an attached B-roll · validated stabilisation (vidstab) · faster render · priority queue for Max tariff ·
preview render (480p) · managed identity for Blob · multi-worker scaling.
