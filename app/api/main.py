from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routers import health, jobs, me, uploads
from app.core.errors import DomainError
from app.core.logging import setup_logging

setup_logging()

MINIAPP_DIR = Path(__file__).resolve().parents[2] / "miniapp"

app = FastAPI(title="Montaj Bot API")
app.include_router(health.router)
app.include_router(me.router)
app.include_router(uploads.router)
app.include_router(jobs.router)


@app.exception_handler(DomainError)
async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(exc.to_body(), status_code=exc.http_status)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, __: RequestValidationError) -> JSONResponse:
    body = {"error": {"code": "VALIDATION_ERROR", "message_uz": "So‘rov noto‘g‘ri."}}
    return JSONResponse(body, status_code=422)


if MINIAPP_DIR.is_dir():
    app.mount("/app", StaticFiles(directory=MINIAPP_DIR, html=True), name="miniapp")
