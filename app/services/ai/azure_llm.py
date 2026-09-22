"""Azure OpenAI (a GlobalStandard chat-completions deployment, e.g. gpt-5-mini) implementation of the same
`LLMClient` protocol as `GeminiClient` (app/services/ai/llm.py). Used when LLM_PROVIDER=azure.

Unlike Gemini, Azure's OpenAI-compatible models cannot ingest a video file directly: `analyze_video()`
extracts ONE representative frame per scene (ffmpeg, at the scene midpoint) and sends them as images
alongside the same per-scene transcript text Gemini gets (`azure_analysis_system.md` tells the model it is
seeing single frames, not full motion, so it leans on the transcript for content judgement). `plan()`/
`revise()` are pure-JSON calls with no visual input, so the existing Gemini prompts (planner_system.md,
revision_system.md) are reused unchanged.

Verified against a real Azure AI Foundry deployment (gpt-5-mini, GlobalStandard, api-version 2024-10-21):
- it is a reasoning model: `temperature` must be omitted entirely (anything but the implicit default 1 is a
  400 "Unsupported value"); `reasoning_effort` controls both quality and latency/cost instead.
- plain `response_format: {"type": "json_object"}` produced syntactically valid JSON but semantically wrong
  enum values (e.g. "top_center" for a field whose only valid values are top/middle/bottom) often enough to
  exhaust the retry-with-feedback loop. Switched to `{"type": "json_schema", "strict": true}` with the
  Pydantic model's own schema (via `_strict_schema`, below) - Azure enforces the schema at decode time, so
  an invalid enum value is no longer possible; verified with a real call.
- vision (image_url data: URIs) and strict JSON-schema mode work together in one request.
"""

import asyncio
import base64
import json
import logging
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import AIError
from app.schemas.analysis import VideoAnalysis
from app.schemas.edit_plan import EditPlan
from app.services.ai.llm import (
    ANALYSIS_USER,
    FEEDBACK_ERROR_CHARS,
    MAX_MESSAGE_CHARS,
    RevisionResult,
    UsageInfo,
    load_prompt,
    strip_fences,
)
from app.services.media.ffmpeg import run_ffmpeg

logger = logging.getLogger(__name__)

API_VERSION = "2024-10-21"  # VERIFY: works against gpt-5-mini on a real GlobalStandard deployment
MAX_API_ATTEMPTS = 3
MAX_VALIDATION_ATTEMPTS = 3
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
FRAME_TIMEOUT_SEC = 30
MAX_FRAMES_PER_CALL = 24  # caps request size/cost on chunks with many scenes
MAX_PROFILE_CHARS = 120

T = TypeVar("T")


def _one_line(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _strict_node(node: Any) -> Any:
    """Recursively rewrite one JSON-schema node into OpenAI/Azure structured-outputs' "strict" subset:
    every object must list ALL its properties as required (a field with a Pydantic default is still valid
    to receive an explicit value - this does not change parsing) and forbid extra properties."""
    if isinstance(node, list):
        return [_strict_node(item) for item in node]
    if not isinstance(node, dict):
        return node
    node = dict(node)
    node.pop("default", None)
    props = node.get("properties")
    if props is not None:
        node["properties"] = {key: _strict_node(value) for key, value in props.items()}
        node["required"] = list(props.keys())
        node["additionalProperties"] = False
    if "items" in node:
        node["items"] = _strict_node(node["items"])
    for key in ("anyOf", "oneOf", "allOf"):
        if key in node:
            node[key] = _strict_node(node[key])
    return node


def _strict_schema(model_cls: type[BaseModel], name: str) -> dict[str, Any]:
    """`response_format` value for `model_cls`, strict-mode JSON schema (see `_strict_node`)."""
    raw = model_cls.model_json_schema()
    defs = raw.pop("$defs", None)
    schema = _strict_node(raw)
    if defs:
        schema["$defs"] = {key: _strict_node(value) for key, value in defs.items()}
    return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}


class AzureOpenAIClient:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timeout: float = 180,
    ) -> None:
        self._settings = settings
        self._client = client
        self._sleep = sleep
        self._timeout = timeout

    # ---------- plumbing ----------

    def _url(self, deployment: str) -> str:
        endpoint = self._settings.azure_openai_endpoint.rstrip("/")
        return f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={API_VERSION}"

    @staticmethod
    def _require(name: str, env_name: str) -> str:
        if not name:
            raise AIError(f"{env_name} is not configured")
        return name

    async def _call(
        self,
        *,
        deployment: str,
        system: str,
        user_content: Any,
        reasoning_effort: str,
        max_tokens: int,
        response_format: dict[str, Any],
    ) -> tuple[str, UsageInfo]:
        if not self._settings.azure_openai_api_key:
            raise AIError("AZURE_OPENAI_API_KEY is not configured")
        self._require(self._settings.azure_openai_endpoint, "AZURE_OPENAI_ENDPOINT")
        payload = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "max_completion_tokens": max_tokens,
            "reasoning_effort": reasoning_effort,
            "response_format": response_format,
        }
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            for attempt in range(1, MAX_API_ATTEMPTS + 1):
                try:
                    response = await client.post(
                        self._url(deployment),
                        headers={"api-key": self._settings.azure_openai_api_key},
                        json=payload,
                    )
                except httpx.TransportError as exc:
                    if attempt == MAX_API_ATTEMPTS:
                        raise AIError(f"Azure OpenAI request failed: {type(exc).__name__}") from exc
                else:
                    if response.status_code == 200:
                        data = response.json()
                        content = data["choices"][0]["message"].get("content") or ""
                        usage = data.get("usage", {})
                        info = UsageInfo(
                            input_tokens=usage.get("prompt_tokens", 0),
                            output_tokens=usage.get("completion_tokens", 0),
                        )
                        return content, info
                    if response.status_code not in TRANSIENT_STATUSES or attempt == MAX_API_ATTEMPTS:
                        raise AIError(
                            f"Azure OpenAI returned HTTP {response.status_code}: {response.text[:300]}"
                        )
                logger.warning("Azure OpenAI transient HTTP error, retry %s/%s", attempt, MAX_API_ATTEMPTS)
                await self._sleep(2 ** (attempt - 1))
        finally:
            if self._client is None:
                await client.aclose()
        raise AIError("Azure OpenAI request failed")  # pragma: no cover (loop always returns or raises)

    async def _structured(
        self,
        *,
        deployment: str,
        system: str,
        user_content_fn: Callable[[str], Any],
        parse: Callable[[str], T],
        reasoning_effort: str,
        max_tokens: int,
        response_model: type[BaseModel],
        schema_name: str,
    ) -> tuple[T, UsageInfo]:
        """Call the model, validate the JSON; on invalid output retry (<= 2x) with the error as feedback.
        `response_format` is the model's own strict JSON schema, so an invalid enum/type is not possible -
        the retry loop only has to handle a rare cross-field validator failure (e.g. src_out > src_in)."""
        response_format = _strict_schema(response_model, schema_name)
        usage = UsageInfo()
        feedback = ""
        for _ in range(MAX_VALIDATION_ATTEMPTS):
            try:
                text, used = await self._call(
                    deployment=deployment,
                    system=system,
                    user_content=user_content_fn(feedback),
                    reasoning_effort=reasoning_effort,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
            except AIError as exc:
                exc.usage = usage
                raise
            usage += used
            try:
                return parse(strip_fences(text)), usage
            except ValueError as exc:  # pydantic.ValidationError is a ValueError
                error = _one_line(str(exc), FEEDBACK_ERROR_CHARS)
                logger.warning("Azure OpenAI returned invalid JSON: %s", error[:200])
                feedback = f"\n\nYour previous JSON was invalid: {error}. Return corrected JSON only."
        raise AIError("Azure OpenAI returned invalid JSON after retries", usage=usage)

    # ---------- frames (Azure has no native video understanding, unlike Gemini) ----------

    async def _extract_frame(self, video_path: Path, timestamp: float, out_path: Path) -> None:
        await run_ffmpeg(
            ["-ss", f"{timestamp:.3f}", "-i", str(video_path), "-frames:v", "1", "-q:v", "3", str(out_path)],
            timeout=FRAME_TIMEOUT_SEC,
        )

    async def _frame_parts(self, video_path: Path, scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="azure_frames_") as tmp:
            tmp_path = Path(tmp)
            for scene in scenes[:MAX_FRAMES_PER_CALL]:
                mid = (scene["start"] + scene["end"]) / 2
                out = tmp_path / f"scene_{scene['scene_id']}.jpg"
                try:
                    await self._extract_frame(video_path, mid, out)
                except Exception:
                    logger.warning("frame extraction failed scene_id=%s", scene["scene_id"], exc_info=True)
                    continue
                b64 = base64.b64encode(out.read_bytes()).decode()
                parts.append({"type": "text", "text": f"(scene_id={scene['scene_id']})"})
                parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        return parts

    # ---------- public API (same shape as GeminiClient) ----------

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
        deployment = self._require(self._settings.azure_analysis_deployment, "AZURE_ANALYSIS_DEPLOYMENT")
        frame_parts = await self._frame_parts(video_path, scenes)
        user_text = ANALYSIS_USER.substitute(
            window_start_s=f"{window[0]:.1f}",
            window_end_s=f"{window[1]:.1f}",
            scenes_json=_dumps(scenes),
            transcript_per_scene_json=_dumps(transcript_by_scene),
            niche=_one_line(niche, MAX_PROFILE_CHARS),
            purpose=_one_line(purpose, MAX_PROFILE_CHARS),
        )

        def content(feedback: str) -> list[dict[str, Any]]:
            return [{"type": "text", "text": user_text + feedback}, *frame_parts]

        max_tokens = min(16000, 400 * max(1, len(scenes)) + 1000)
        return await self._structured(
            deployment=deployment,
            system=load_prompt("azure_analysis_system.md"),
            user_content_fn=content,
            parse=VideoAnalysis.model_validate_json,
            reasoning_effort="low",
            max_tokens=max_tokens,
            response_model=VideoAnalysis,
            schema_name="VideoAnalysis",
        )

    async def plan(self, *, context: dict[str, Any]) -> tuple[EditPlan, UsageInfo]:
        deployment = self._require(self._settings.azure_planner_deployment, "AZURE_PLANNER_DEPLOYMENT")
        base_text = _dumps(context)
        return await self._structured(
            deployment=deployment,
            system=load_prompt("planner_system.md"),
            user_content_fn=lambda feedback: base_text + feedback,
            parse=EditPlan.model_validate_json,
            reasoning_effort="medium",
            max_tokens=8000,
            response_model=EditPlan,
            schema_name="EditPlan",
        )

    async def revise(
        self, *, current_plan: EditPlan, message: str, context: dict[str, Any]
    ) -> tuple[EditPlan, list[str], list[str], UsageInfo]:
        deployment = self._require(self._settings.azure_planner_deployment, "AZURE_PLANNER_DEPLOYMENT")
        base_text = _dumps(
            {
                "current_plan": current_plan.model_dump(),
                "creator_message": message[:MAX_MESSAGE_CHARS],
                "context": context,
            }
        )
        result, usage = await self._structured(
            deployment=deployment,
            system=load_prompt("revision_system.md"),
            user_content_fn=lambda feedback: base_text + feedback,
            parse=RevisionResult.model_validate_json,
            reasoning_effort="medium",
            max_tokens=8000,
            response_model=RevisionResult,
            schema_name="RevisionResult",
        )
        return result.plan, result.changes_uz, result.unsupported_uz, usage

    async def release_video(self, video_path: Path) -> None:
        return (
            None  # no server-side file to clean up: frames are extracted fresh into an auto-cleaned temp dir
        )
