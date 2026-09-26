"""Gemini wrapper (`google-genai`) behind the `LLMClient` protocol.

SDK usage verified against google-genai 2.24: `Client(api_key=)`, `client.aio.models.generate_content`,
`client.aio.files.upload/get/delete`, `types.VideoMetadata(start_offset, end_offset, fps)`,
`response.usage_metadata.{prompt_token_count, candidates_token_count, thoughts_token_count}`.
"""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any, Protocol, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import AIError
from app.schemas.analysis import VideoAnalysis
from app.schemas.edit_plan import EditPlan

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
TEMPERATURE_ANALYSIS = 0.2  # RUNTIME_PROMPTS.md section D
TEMPERATURE_PLAN = 0.4
TEMPERATURE_REVISE = 0.3
TEMPERATURE_TRANSCRIPT = 0.0  # a transcription, not a creative task (the benchmark ran at temperature 0)
MAX_API_ATTEMPTS = 3  # transient errors (429/5xx), exponential backoff
MAX_VALIDATION_ATTEMPTS = 3  # first try + 2 retries with feedback
TRANSIENT_CODES = {429, 500, 502, 503, 504}
MAX_MESSAGE_CHARS = 1000
MAX_PROFILE_CHARS = 120
FEEDBACK_ERROR_CHARS = 1500

ANALYSIS_USER = Template(
    "Analyze the window from $window_start_s s to $window_end_s s of the attached video "
    "(timestamps are from the start of the file).\n\n"
    "SCENES (analyze exactly these):\n$scenes_json\n\n"
    "TRANSCRIPT PER SCENE (may contain mistakes; language can be Uzbek, Russian or mixed):\n"
    "$transcript_per_scene_json\n\n"
    "CREATOR PROFILE (context only): niche = $niche; purpose of the video = $purpose\n\n"
    "Return the JSON now."
)

T = TypeVar("T")
_MIME_BY_SUFFIX = {".ogg": "audio/ogg", ".mp4": "video/mp4"}


@dataclass(frozen=True, slots=True)
class UsageInfo:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "UsageInfo") -> "UsageInfo":
        return UsageInfo(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens)


class TranscriptFix(BaseModel):
    text: str


class RevisionResult(BaseModel):
    plan: EditPlan
    changes_uz: list[str] = []
    unsupported_uz: list[str] = []


class LLMClient(Protocol):
    async def analyze_video(
        self,
        *,
        video_path: Path,
        window: tuple[float, float],
        scenes: list[dict[str, Any]],
        transcript_by_scene: list[dict[str, Any]],
        niche: str,
        purpose: str,
        fps: float | None = None,
    ) -> tuple[VideoAnalysis, UsageInfo]: ...

    async def plan(self, *, context: dict[str, Any]) -> tuple[EditPlan, UsageInfo]: ...

    async def revise(
        self, *, current_plan: EditPlan, message: str, context: dict[str, Any]
    ) -> tuple[EditPlan, list[str], list[str], UsageInfo]: ...

    async def correct_transcript(self, *, audio_path: Path, draft: str) -> tuple[str, UsageInfo]: ...

    async def release_video(self, video_path: Path) -> None:
        """Delete any provider-side copy of the video (privacy: used only for the user's own result)."""
        ...


@lru_cache
def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _one_line(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


def strip_fences(text: str) -> str:
    text = text.strip()
    match = _FENCE.match(text)
    return match.group(1) if match else text


def _usage(response: Any) -> UsageInfo:
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return UsageInfo()
    output = (meta.candidates_token_count or 0) + (getattr(meta, "thoughts_token_count", 0) or 0)
    return UsageInfo(input_tokens=meta.prompt_token_count or 0, output_tokens=output)


class GeminiClient:
    def __init__(
        self,
        settings: Settings,
        *,
        client: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_interval_sec: float = 2.0,
        file_ready_timeout_sec: float = 600.0,
    ) -> None:
        self._settings = settings
        self._client = client
        self._sleep = sleep
        self._poll = poll_interval_sec
        self._file_timeout = file_ready_timeout_sec
        self._files: dict[str, Any] = {}
        self._upload_lock = asyncio.Lock()

    # ---------- plumbing ----------

    def _sdk(self) -> Any:
        if self._client is None:
            if not self._settings.gemini_api_key:
                raise AIError("GEMINI_API_KEY is not configured")
            self._client = genai.Client(api_key=self._settings.gemini_api_key)
        return self._client

    @staticmethod
    def _require_model(name: str, env_name: str) -> str:
        if not name:
            raise AIError(f"{env_name} is not configured")
        return name

    def _media_resolution(self) -> Any:
        wanted = f"MEDIA_RESOLUTION_{self._settings.gemini_media_resolution.upper()}"
        return getattr(types.MediaResolution, wanted, None)

    async def _call(
        self, *, model: str, system: str, contents: list[Any], temperature: float, video: bool = False
    ) -> tuple[str, UsageInfo]:
        # Structured output is requested via the prompt + response_mime_type and validated by Pydantic
        # (RUNTIME_PROMPTS.md D: response_schema is optional and not every model accepts the EditPlan schema).
        config_args: dict[str, Any] = {
            "system_instruction": system,
            "temperature": temperature,
            "response_mime_type": "application/json",
        }
        if video and (resolution := self._media_resolution()) is not None:
            config_args["media_resolution"] = resolution
        config = types.GenerateContentConfig(**config_args)

        for attempt in range(1, MAX_API_ATTEMPTS + 1):
            try:
                response = await self._sdk().aio.models.generate_content(
                    model=model, contents=contents, config=config
                )
                return (getattr(response, "text", None) or ""), _usage(response)
            except AIError:
                raise
            except Exception as exc:
                code = getattr(exc, "code", None)
                transient = code in TRANSIENT_CODES or isinstance(exc, TimeoutError | ConnectionError)
                if not transient or attempt == MAX_API_ATTEMPTS:
                    raise AIError(f"Gemini call failed: {type(exc).__name__} code={code}") from exc
                logger.warning("Gemini transient error code=%s, retry %s/%s", code, attempt, MAX_API_ATTEMPTS)
                await self._sleep(2 ** (attempt - 1))
        raise AIError("Gemini call failed")  # pragma: no cover

    async def _structured(
        self,
        *,
        model: str,
        system: str,
        user_text: str,
        temperature: float,
        parse: Callable[[str], T],
        prefix_parts: list[Any] | None = None,
        video: bool = False,
    ) -> tuple[T, UsageInfo]:
        """Call the model, validate the JSON; on invalid output retry (<= 2x) with the error as feedback."""
        usage = UsageInfo()
        feedback = ""
        for _ in range(MAX_VALIDATION_ATTEMPTS):
            contents = [*(prefix_parts or []), user_text + feedback]
            try:
                text, used = await self._call(
                    model=model, system=system, contents=contents, temperature=temperature, video=video
                )
            except AIError as exc:
                exc.usage = usage
                raise
            usage += used
            try:
                return parse(strip_fences(text)), usage
            except ValueError as exc:  # pydantic.ValidationError is a ValueError
                error = _one_line(str(exc), FEEDBACK_ERROR_CHARS)
                logger.warning("Gemini returned invalid JSON: %s", error[:200])
                feedback = f"\n\nYour previous JSON was invalid: {error}. Return corrected JSON only."
        raise AIError("Gemini returned invalid JSON after retries", usage=usage)

    # ---------- video upload (cached per file, deleted after analysis) ----------

    async def _ensure_uploaded(self, video_path: Path) -> Any:
        key = str(video_path)
        async with self._upload_lock:
            if key in self._files:
                return self._files[key]
            sdk = self._sdk()
            try:
                # the slim image's mimetypes table has no .ogg: pass the type (google-genai 2.25 API)
                mime = _MIME_BY_SUFFIX.get(video_path.suffix.lower())
                file = await sdk.aio.files.upload(file=key, config={"mime_type": mime} if mime else None)
                waited = 0.0
                while _state_name(file) == "PROCESSING":
                    if waited >= self._file_timeout:
                        raise AIError("Gemini file processing timed out")
                    await self._sleep(self._poll)
                    waited += self._poll
                    file = await sdk.aio.files.get(name=file.name)
            except AIError:
                raise
            except Exception as exc:
                raise AIError(f"Gemini file upload failed: {type(exc).__name__}") from exc
            if _state_name(file) != "ACTIVE":
                raise AIError(f"Gemini file is not usable (state={_state_name(file)})")
            self._files[key] = file
            return file

    async def correct_transcript(self, *, audio_path: Path, draft: str) -> tuple[str, UsageInfo]:
        """Gemini listens to `audio_path` and returns the corrected text of `draft` (ai/transcript_fix.py)."""
        model = self._require_model(self._settings.gemini_analysis_model, "GEMINI_ANALYSIS_MODEL")
        file = await self._ensure_uploaded(audio_path)
        part = types.Part(
            file_data=types.FileData(file_uri=file.uri, mime_type=file.mime_type or "audio/ogg")
        )
        try:
            result, usage = await self._structured(
                model=model,
                system=load_prompt("transcript_fix_system.md"),
                user_text=_dumps({"draft": draft}),
                temperature=TEMPERATURE_TRANSCRIPT,
                parse=TranscriptFix.model_validate_json,
                prefix_parts=[part],
            )
        finally:
            await self.release_video(audio_path)
        return result.text, usage

    async def release_video(self, video_path: Path) -> None:
        file = self._files.pop(str(video_path), None)
        if file is None:
            return
        try:
            await self._sdk().aio.files.delete(name=file.name)
        except Exception:
            logger.warning("could not delete the uploaded Gemini file", exc_info=True)

    # ---------- public API ----------

    async def analyze_video(
        self,
        *,
        video_path: Path,
        window: tuple[float, float],
        scenes: list[dict[str, Any]],
        transcript_by_scene: list[dict[str, Any]],
        niche: str,
        purpose: str,
        fps: float | None = None,
    ) -> tuple[VideoAnalysis, UsageInfo]:
        model = self._require_model(self._settings.gemini_analysis_model, "GEMINI_ANALYSIS_MODEL")
        file = await self._ensure_uploaded(video_path)
        part = types.Part(
            file_data=types.FileData(file_uri=file.uri, mime_type=file.mime_type or "video/mp4"),
            video_metadata=types.VideoMetadata(
                start_offset=f"{window[0]:.3f}s",
                end_offset=f"{window[1]:.3f}s",
                fps=fps or self._settings.gemini_analysis_fps,
            ),
        )
        user_text = ANALYSIS_USER.substitute(
            window_start_s=f"{window[0]:.1f}",
            window_end_s=f"{window[1]:.1f}",
            scenes_json=_dumps(scenes),
            transcript_per_scene_json=_dumps(transcript_by_scene),
            niche=_one_line(niche, MAX_PROFILE_CHARS),
            purpose=_one_line(purpose, MAX_PROFILE_CHARS),
        )
        return await self._structured(
            model=model,
            system=load_prompt("analysis_system.md"),
            user_text=user_text,
            temperature=TEMPERATURE_ANALYSIS,
            parse=VideoAnalysis.model_validate_json,
            prefix_parts=[part],
            video=True,
        )

    async def plan(self, *, context: dict[str, Any]) -> tuple[EditPlan, UsageInfo]:
        model = self._require_model(self._settings.gemini_planner_model, "GEMINI_PLANNER_MODEL")
        return await self._structured(
            model=model,
            system=load_prompt("planner_system.md"),
            user_text=_dumps(context),
            temperature=TEMPERATURE_PLAN,
            parse=EditPlan.model_validate_json,
        )

    async def revise(
        self, *, current_plan: EditPlan, message: str, context: dict[str, Any]
    ) -> tuple[EditPlan, list[str], list[str], UsageInfo]:
        model = self._require_model(self._settings.gemini_planner_model, "GEMINI_PLANNER_MODEL")
        user_text = _dumps(
            {
                "current_plan": current_plan.model_dump(),
                "creator_message": message[:MAX_MESSAGE_CHARS],
                "context": context,
            }
        )
        result, usage = await self._structured(
            model=model,
            system=load_prompt("revision_system.md"),
            user_text=user_text,
            temperature=TEMPERATURE_REVISE,
            parse=RevisionResult.model_validate_json,
        )
        return result.plan, result.changes_uz, result.unsupported_uz, usage


def _state_name(file: Any) -> str:
    state = getattr(file, "state", None)
    return str(getattr(state, "name", state) or "")
