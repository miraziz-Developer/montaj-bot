"""Analyse a LOCAL video and print the resulting EditPlan JSON (no Telegram, no Postgres, no Blob).

For fast iteration on prompts. Uses the REAL Gemini provider when GEMINI_API_KEY (+ models) are set, and the
REAL STT provider configured via STT_PROVIDER (groq/azure, with its own key); otherwise an empty transcript
and the deterministic fallback planner.

    docker compose run --rm api python scripts/dev_analyze.py /app/some_video.mp4 --style dynamic_reels
"""

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import AIError
from app.core.logging import setup_logging
from app.models import Job, Upload
from app.schemas.analysis import SceneAnalysis, VideoAnalysis
from app.services.ai.analysis import analyze_full_video
from app.services.ai.llm import GeminiClient, UsageInfo
from app.services.ai.plan_text import PlanContext, SourceInfo
from app.services.ai.planner import build_initial_plan
from app.services.media.pipeline import run_pre_analysis
from app.services.media.probe import probe
from app.services.render.music import load_music_catalog
from app.services.stt.base import STTProvider, Transcript
from app.services.stt.fake import FakeSTTProvider
from app.worker.main import build_stt


class MemoryStorage:
    """Just enough BlobStorage for run_pre_analysis: everything lives in a dict."""

    def __init__(self) -> None:
        self.blobs: dict[tuple[str, str], bytes] = {}

    async def get_blob_size(self, container: str, blob_path: str) -> int | None:
        data = self.blobs.get((container, blob_path))
        return None if data is None else len(data)

    async def download_to_file(self, container: str, blob_path: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.blobs[(container, blob_path)])

    async def upload_file(self, container: str, blob_path: str, src: Path, content_type: str) -> None:
        self.blobs[(container, blob_path)] = src.read_bytes()

    async def delete_blob(self, container: str, blob_path: str) -> None:
        self.blobs.pop((container, blob_path), None)

    async def list_old_blobs(self, container: str, older_than: timedelta) -> list[str]:
        return []


class NoLLM:
    """Stands in for Gemini when no key is configured: every call fails, so the fallback planner runs."""

    async def analyze_video(self, **_kw: object) -> tuple[VideoAnalysis, UsageInfo]:
        raise AIError("no LLM configured")

    async def plan(self, **_kw: object) -> tuple[object, UsageInfo]:
        raise AIError("no LLM configured")

    async def revise(self, **_kw: object) -> tuple[object, list[str], list[str], UsageInfo]:
        raise AIError("no LLM configured")

    async def release_video(self, _video_path: Path) -> None:
        return None


async def analyze(args: argparse.Namespace) -> dict:
    settings = get_settings()
    info = await probe(str(args.video))
    user_id, upload_id = uuid.uuid4(), uuid.uuid4()
    upload = Upload(
        id=upload_id, user_id=user_id, blob_path=f"{user_id}/{upload_id}/source{args.video.suffix}",
        original_filename=args.video.name, content_type="video/mp4", size_bytes=args.video.stat().st_size,
        duration_sec=info.duration_sec, width=info.width, height=info.height, fps=info.fps,
        has_audio=info.has_audio, video_codec=info.video_codec,
    )  # fmt: skip
    job = Job(
        id=uuid.uuid4(), user_id=user_id, upload_id=upload_id, aspect=args.aspect, style_preset=args.style,
        brief=args.brief, is_trial=args.trial,
    )  # fmt: skip
    storage = MemoryStorage()
    storage.blobs[(settings.azure_uploads_container, upload.blob_path)] = args.video.read_bytes()

    stt_configured = (settings.stt_provider == "groq" and settings.groq_api_key) or (
        settings.stt_provider == "azure" and settings.azure_speech_api_key and settings.azure_speech_endpoint
    )
    stt: STTProvider = (
        build_stt(settings)
        if stt_configured
        else FakeSTTProvider(default=Transcript(language="", segments=[]))
    )
    live_llm = bool(
        settings.gemini_api_key and settings.gemini_analysis_model and settings.gemini_planner_model
    )
    gemini = GeminiClient(settings) if live_llm else NoLLM()

    workdir = Path(tempfile.mkdtemp(prefix="dev_analyze_"))
    try:
        pre = await run_pre_analysis(job, upload, workdir, storage, stt, settings)  # type: ignore[arg-type]
        try:
            analysis, usage = await analyze_full_video(
                gemini, proxy_path=pre.proxy_path, scenes=pre.scenes, transcript=pre.transcript,
                niche=args.niche, purpose=args.purpose, chunk_sec=settings.analysis_chunk_sec,
            )  # fmt: skip
        except AIError as exc:
            print(f"analysis unavailable ({exc.detail}): using neutral defaults", file=sys.stderr)
            analysis = VideoAnalysis(scenes=[SceneAnalysis(scene_id=s.scene_id) for s in pre.scenes])
            usage = UsageInfo()
        ctx = PlanContext(
            source=SourceInfo(
                info.duration_sec, info.width, info.height, info.has_audio, bool(pre.transcript.all_words())
            ),
            analysis=analysis, scenes=pre.scenes, transcript=pre.transcript, silences=pre.silences,
            music_tracks=load_music_catalog(settings.assets_dir),
            creator_profile={"niche": args.niche, "purpose": args.purpose},
        )  # fmt: skip
        plan, source, plan_usage = await build_initial_plan(gemini, job=job, ctx=ctx)  # type: ignore[arg-type]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    tokens = usage + (plan_usage or UsageInfo())
    return {
        "planner": source.value,
        "live_llm": live_llm,
        "tokens": {"input": tokens.input_tokens, "output": tokens.output_tokens},
        "scenes": len(pre.scenes),
        "words": len(pre.transcript.all_words()),
        "plan": plan.model_dump(mode="json"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("video", type=Path)
    parser.add_argument("--aspect", default="9:16", choices=["9:16", "16:9", "1:1", "original"])
    parser.add_argument(
        "--style",
        default="dynamic_reels",
        choices=["dynamic_reels", "clean_talk", "ad_commercial", "vlog_story"],
    )
    parser.add_argument("--brief", default="")
    parser.add_argument("--niche", default="")
    parser.add_argument("--purpose", default="")
    parser.add_argument("--trial", action="store_true", help="plan as a trial job (adds the watermark)")
    args = parser.parse_args()
    setup_logging()
    print(json.dumps(asyncio.run(analyze(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
