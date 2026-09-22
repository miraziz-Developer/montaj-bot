# AGENTS.md — Rules for the coding agent (read before EVERY task)

Project: "Montaj Bot" — a Telegram bot + Telegram Mini App that edits users' videos with AI.
Users upload a video (Mini App -> Azure Blob), AI analyzes it, proposes an edit plan (JSON), the user
approves or revises it in chat, then a worker renders the final video with FFmpeg and sends it back.

Source of truth for design: `docs/ARCHITECTURE.md`, `docs/EDIT_PLAN_SCHEMA.md`, `docs/RUNTIME_PROMPTS.md`.
If a task prompt and these docs disagree, the docs win. Tell the user about the conflict at the end.

## 0. How you must work
1. Read AGENTS.md, docs/ARCHITECTURE.md and every file the task mentions BEFORE writing code.
2. Start every task by printing a short numbered plan (max 10 lines). Then implement it.
3. Implement ONLY the current task. Do not start later phases. Do not refactor unrelated files.
4. If something is ambiguous, pick the simplest option, add `# ASSUMPTION: ...` in the code, and list all
   assumptions at the end of your answer.
5. NEVER invent library APIs. If you are not 100% sure about a function signature of aiogram, google-genai,
   azure-storage-blob, arq, PySceneDetect or ffmpeg options: inspect the installed package
   (`python -c "import x; help(x.y)"`) or read the official docs, and if still unsure write the smallest safe
   version and mark it `# VERIFY`.
6. After coding run `make lint` and `make test`. Fix failures. In your final answer report what you really ran
   and the real result. NEVER claim tests pass if you did not run them.
7. End every task with: (a) files created/changed, (b) how to run/verify, (c) assumptions, (d) known limitations.
8. Keep files under ~400 lines. Split into modules if bigger.

## 1. Stack (do not change unless asked)
Python 3.12 · FastAPI · aiogram 3.x · SQLAlchemy 2.x (async, asyncpg) · Alembic · Pydantic v2 + pydantic-settings ·
PostgreSQL 16 · Redis 7 · arq · httpx · azure-storage-blob (with aio) · google-genai · ffmpeg/ffprobe (system binary) ·
PySceneDetect · pytest + pytest-asyncio · ruff. Mini App = plain HTML/CSS/JS (NO framework, NO bundler).
Do NOT add Celery, Django, Flask, Docker Swarm, Kubernetes, or any ORM other than SQLAlchemy.

## 2. Code rules
- Type hints everywhere. Small functions. No global mutable state. Prefer dataclasses/Pydantic models over dicts.
- API and bot code is async. Blocking work (ffmpeg, file IO) uses `asyncio.create_subprocess_exec` or `asyncio.to_thread`.
- Subprocess: ALWAYS a list of args, NEVER `shell=True`. Capture stderr; on failure raise an error containing the last
  2000 chars of stderr. Always set a timeout.
- All config via `app/core/config.py` (pydantic-settings). No secrets or magic numbers in code.
  `.env.example` must list every variable.
- Code, comments, logs, identifiers: English. User-facing bot/Mini App texts: Uzbek (Latin script, use o‘ and g‘
  with the ‘ character), stored in `app/bot/texts.py` and `miniapp/i18n.js`. Use the exact Uzbek strings given in prompts.
- Logging: stdlib `logging`, one logger per module, include `job_id`/`user_id` in messages.
  Never log tokens, SAS URLs, initData, phone numbers.
- DB: every units/money change happens in ONE transaction with `SELECT ... FOR UPDATE` on the user row.
  The ledger (`unit_ledger`) is append-only. Never edit or delete ledger rows.
- Idempotency: worker stages must be safe to re-run. Check whether an artifact already exists before recomputing.
- Errors: raise domain exceptions from `app/core/errors.py`; convert them to HTTP/Telegram messages only at the edges.
  API error body is always `{"error": {"code": "SOME_CODE", "message_uz": "..."}}`.

## 3. Security rules (non-negotiable)
- Every Mini App API request is authenticated by verifying Telegram initData (HMAC) on the server.
  Never trust a user id sent in a request body or query string.
- SAS URLs: write-only for uploads, read-only for downloads, one blob only, short expiry.
- NEVER put user-provided text into an ffmpeg filter string. Captions/overlays/watermark text go into an ASS file
  after escaping (see docs/EDIT_PLAN_SCHEMA.md, section "Text safety").
- Validate uploaded media with ffprobe on the server. Reject: no video stream, duration/size over limits.
- Never delete user data except by the retention job or an explicit user action.
- Temp dirs are always removed in `finally` blocks.

## 4. Testing rules
- Every service gets unit tests. External services (Telegram, Azure, Gemini, Groq) are mocked behind interfaces
  (Protocol classes) with in-memory fakes in `tests/fakes/`.
- FFmpeg tests use a synthetic video created in a pytest fixture (3–6 seconds, small), e.g.
  `ffmpeg -f lavfi -i testsrc=size=640x360:rate=30 -f lavfi -i sine=frequency=440 -t 5 ...`.
- Assert on outputs (ffprobe duration/resolution/streams, file exists), not on implementation details.
- Tests must not need real API keys or internet.

## 5. Definition of Done
- [ ] Code runs, `make lint` clean
- [ ] `make test` green (new tests included)
- [ ] New env vars are in `.env.example` and `config.py`
- [ ] docs/ARCHITECTURE.md changed ONLY if you changed a contract (and you said so)
- [ ] Final report follows section 0.7

## 6. If you get stuck
Do not loop. After 2 failed attempts at the same error: stop, print the exact error, what you tried, and your best
hypothesis. Do not "fix" tests by weakening assertions.
