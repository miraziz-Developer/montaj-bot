import asyncio
import logging
import re
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

_STDERR_TAIL_CHARS = 2000
# SAS tokens live in the query string; never let them reach logs or error messages.
_URL_QUERY = re.compile(r"(https?://[^\s'\"?]+)\?[^\s'\"]+")


class FFmpegError(Exception):
    """A media subprocess failed, timed out, or could not start."""


def redact_urls(text: str) -> str:
    return _URL_QUERY.sub(r"\1?<redacted>", text)


async def run_ffmpeg(
    args: Sequence[str], *, timeout: float, cwd: Path | str | None = None, check: bool = True
) -> tuple[int, str, str]:
    """`ffmpeg -hide_banner -nostdin <args>`; see `run` for the error contract."""
    return await run(["ffmpeg", "-hide_banner", "-nostdin", *args], timeout, cwd, check=check)


async def run_ffprobe(
    args: Sequence[str], *, timeout: float = 60, cwd: Path | str | None = None
) -> tuple[int, str, str]:
    return await run(["ffprobe", "-v", "error", *args], timeout, cwd)


async def run(
    args: Sequence[str], timeout: float, cwd: Path | str | None = None, *, check: bool = True
) -> tuple[int, str, str]:
    """Run a command (list of args, never a shell). Returns (returncode, stdout, stderr).

    Raises FFmpegError on timeout, and on a non-zero exit when `check` is true (message holds the last
    2000 chars of stderr).
    """
    name = str(args[0])
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except OSError as exc:
        raise FFmpegError(f"{name} could not start: {exc}") from exc

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise FFmpegError(f"{name} timed out after {timeout:g}s") from None

    stdout = out.decode(errors="replace")
    stderr = err.decode(errors="replace")
    returncode = proc.returncode if proc.returncode is not None else -1
    if check and returncode != 0:
        tail = redact_urls(stderr)[-_STDERR_TAIL_CHARS:]
        raise FFmpegError(f"{name} exited with code {returncode}: {tail}")
    return returncode, stdout, stderr
