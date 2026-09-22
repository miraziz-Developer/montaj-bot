"""AzureOpenAIClient against a fake HTTP transport (httpx.MockTransport, no network)."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AIError
from app.schemas.edit_plan import EditPlan
from app.services.ai.azure_llm import AzureOpenAIClient, _strict_schema
from tests.fakes.gemini import make_plan

PLAN_JSON = make_plan([(0.0, 5.0)]).model_dump_json()
SCENES = [{"scene_id": 1, "start": 0.0, "end": 4.0}, {"scene_id": 2, "start": 4.0, "end": 8.0}]
ANALYSIS_JSON = json.dumps(
    {
        "overall": {"has_speech": True},
        "scenes": [
            {"scene_id": 1, "description": "car"},
            {"scene_id": 2, "description": "road"},
        ],
    }
)


def _settings(**overrides: object) -> Settings:
    defaults = dict(
        _env_file=None,
        llm_provider="azure",
        azure_openai_api_key="SECRET-KEY",
        azure_openai_endpoint="https://proj.cognitiveservices.azure.com",
        azure_analysis_deployment="gpt-5-mini",
        azure_planner_deployment="gpt-5-mini",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _completion(content: str, prompt: int = 100, completion: int = 50) -> dict:
    return {
        "choices": [{"message": {"content": content, "role": "assistant"}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


def _client(handler, sleeps: list[float] | None = None, **settings_overrides: object) -> AzureOpenAIClient:
    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AzureOpenAIClient(_settings(**settings_overrides), client=http_client, sleep=sleep)


# ---------- _strict_schema(): the strict-mode JSON schema transform ----------


def _walk_all(node: object) -> list[dict]:
    """Every dict node in a schema tree, for asserting a property holds everywhere."""
    found = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found.extend(_walk_all(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_all(item))
    return found


def test_strict_schema_forbids_extra_properties_and_requires_all_fields_everywhere() -> None:
    response_format = _strict_schema(EditPlan, "EditPlan")
    assert response_format == {
        "type": "json_schema",
        "json_schema": {
            "name": "EditPlan",
            "strict": True,
            "schema": response_format["json_schema"]["schema"],
        },
    }
    schema = response_format["json_schema"]["schema"]
    for node in _walk_all(schema):
        if "properties" in node:
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            assert "default" not in node


def test_strict_schema_preserves_enum_constraints() -> None:
    schema = _strict_schema(EditPlan, "EditPlan")["json_schema"]["schema"]
    clip = schema["$defs"]["Clip"]
    assert clip["properties"]["role"]["enum"] == [
        "hook", "body", "broll", "cta", "outro", "filler",
    ]  # fmt: skip


# ---------- plan() / revise(): pure JSON, no frames ----------


async def test_plan_request_shape_and_no_temperature() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion(PLAN_JSON))

    plan, usage = await _client(handler).plan(context={"a": 1})
    [request] = seen
    assert request.headers["api-key"] == "SECRET-KEY"
    assert str(request.url) == (
        "https://proj.cognitiveservices.azure.com/openai/deployments/gpt-5-mini/chat/completions"
        "?api-version=2024-10-21"
    )
    body = json.loads(request.content)
    assert "temperature" not in body
    assert body["reasoning_effort"] == "medium"
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "EditPlan"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False
    assert body["messages"][0]["role"] == "system"
    assert '"a": 1' in body["messages"][1]["content"]
    assert plan.title == "Test"
    assert (usage.input_tokens, usage.output_tokens) == (100, 50)


async def test_plan_retries_invalid_json_with_feedback() -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(200, json=_completion("{not valid json"))
        return httpx.Response(200, json=_completion(PLAN_JSON))

    plan, _ = await _client(handler).plan(context={})
    assert len(calls) == 2
    assert "was invalid" in calls[1]["messages"][1]["content"]
    assert plan.title == "Test"


async def test_plan_gives_up_after_repeated_invalid_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("not json at all"))

    with pytest.raises(AIError, match="invalid JSON"):
        await _client(handler).plan(context={})


async def test_revise_uses_revision_prompt_and_message_is_truncated() -> None:
    from tests.fakes.gemini import make_plan as _mp

    revision_json = json.dumps(
        {"plan": json.loads(PLAN_JSON), "changes_uz": ["o'zgardi"], "unsupported_uz": []}
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion(revision_json))

    plan, changes, unsupported, _ = await _client(handler).revise(
        current_plan=_mp([(0.0, 5.0)]), message="x" * 2000, context={}
    )
    body = json.loads(seen[0].content)
    sent = json.loads(body["messages"][1]["content"])
    assert len(sent["creator_message"]) == 1000
    assert changes == ["o'zgardi"] and unsupported == []
    assert plan.title == "Test"


# ---------- HTTP behaviour (retries, config errors) ----------


async def test_retries_5xx_then_succeeds() -> None:
    calls, sleeps = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json=_completion(PLAN_JSON))

    await _client(handler, sleeps).plan(context={})
    assert len(calls) == 3 and sleeps == [1, 2]


async def test_gives_up_after_three_attempts_on_429() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, text="rate limited")

    with pytest.raises(AIError, match="429"):
        await _client(handler).plan(context={})
    assert len(calls) == 3


async def test_client_errors_are_not_retried_and_never_leak_the_key() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, text="invalid api key")

    with pytest.raises(AIError) as exc:
        await _client(handler).plan(context={})
    assert len(calls) == 1 and "SECRET-KEY" not in str(exc.value)


async def test_without_an_api_key_fails_clearly_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent without a key")

    with pytest.raises(AIError, match="AZURE_OPENAI_API_KEY"):
        await _client(handler, azure_openai_api_key="").plan(context={})


async def test_without_a_deployment_fails_clearly_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent without a deployment")

    with pytest.raises(AIError, match="AZURE_PLANNER_DEPLOYMENT"):
        await _client(handler, azure_planner_deployment="").plan(context={})


# ---------- analyze_video(): frame extraction + vision ----------


async def test_analyze_video_sends_one_frame_per_scene_and_uses_low_reasoning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    extracted: list[tuple[float, Path]] = []

    async def fake_extract(
        self: AzureOpenAIClient, video_path: Path, timestamp: float, out_path: Path
    ) -> None:
        extracted.append((timestamp, out_path))
        out_path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    monkeypatch.setattr(AzureOpenAIClient, "_extract_frame", fake_extract)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion(ANALYSIS_JSON))

    analysis, _ = await _client(handler).analyze_video(
        video_path=tmp_path / "proxy.mp4",
        window=(0.0, 8.0),
        scenes=SCENES,
        transcript_by_scene=[{"scene_id": 1, "text": "hi"}, {"scene_id": 2, "text": ""}],
        niche="",
        purpose="",
    )
    assert [t for t, _ in extracted] == [2.0, 6.0]  # scene midpoints
    body = json.loads(seen[0].content)
    assert body["reasoning_effort"] == "low"
    user_content = body["messages"][1]["content"]
    kinds = [part["type"] for part in user_content]
    assert kinds.count("image_url") == 2
    assert any("scene_id=1" in p.get("text", "") for p in user_content if p["type"] == "text")
    assert len(analysis.scenes) == 2


async def test_analyze_video_skips_scenes_whose_frame_extraction_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def flaky_extract(
        self: AzureOpenAIClient, video_path: Path, timestamp: float, out_path: Path
    ) -> None:
        if timestamp == 2.0:
            raise RuntimeError("ffmpeg exploded")
        out_path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    monkeypatch.setattr(AzureOpenAIClient, "_extract_frame", flaky_extract)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(ANALYSIS_JSON))

    client = _client(handler)
    parts = await client._frame_parts(tmp_path / "proxy.mp4", SCENES)
    image_parts = [p for p in parts if p["type"] == "image_url"]
    text_labels = [p["text"] for p in parts if p["type"] == "text"]
    assert len(image_parts) == 1  # scene 1's frame failed and was skipped
    assert text_labels == ["(scene_id=2)"]


async def test_analyze_video_caps_frames_per_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = AsyncMock()

    async def counting_extract(
        self: AzureOpenAIClient, video_path: Path, timestamp: float, out_path: Path
    ) -> None:
        await calls()
        out_path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    monkeypatch.setattr(AzureOpenAIClient, "_extract_frame", counting_extract)
    many_scenes = [{"scene_id": i, "start": float(i), "end": float(i + 1)} for i in range(40)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(ANALYSIS_JSON))

    client = _client(handler)
    await client._frame_parts(tmp_path / "proxy.mp4", many_scenes)
    assert calls.await_count == 24  # MAX_FRAMES_PER_CALL
