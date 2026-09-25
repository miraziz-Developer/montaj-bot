import subprocess
from pathlib import Path

import pytest

from app.schemas.edit_plan import Captions, EditPlan, Sfx, Sticker
from app.services.ai.llm import load_prompt
from app.services.ai.plan_validator import force_job_settings, validate_plan
from app.services.ai.presets import get_preset
from app.services.media.probe import probe
from app.services.render.final_stage import _video_graph, place_stickers, render_final
from app.services.render.sfx import plan_cues
from app.services.render.stickers import STICKERS, PlacedSticker, sticker_file
from tests.fakes.gemini import make_plan

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "assets"


def _plan(**fields) -> EditPlan:  # noqa: ANN003
    return make_plan([(0, 6)], **fields)


# ---------- catalog ----------


def test_every_catalog_sticker_has_a_transparent_image_and_the_prompt_lists_them_all() -> None:
    for name in STICKERS:
        path = sticker_file(ASSETS, name)
        assert path is not None, name
        assert path.read_bytes()[25] == 6, f"{name}.png must be RGBA"  # PNG colour type 6
    prompt = load_prompt("planner_system.md")
    assert all(f'"{name}"' in prompt for name in STICKERS)
    assert sticker_file(ASSETS, "../music/catalog") is None  # only catalog names resolve


# ---------- schema / validation ----------


def test_unknown_names_are_dropped_instead_of_failing_the_plan() -> None:
    plan = EditPlan.model_validate(
        {**_plan().model_dump(), "look": "neon",
         "stickers": [{"emoji": "fire", "start": 1, "end": 2}, {"emoji": "unicorn", "start": 1, "end": 2}]}
    )  # fmt: skip
    assert plan.look == "natural" and [s.emoji for s in plan.stickers] == ["fire"]


def test_validator_trims_caps_and_separates_stickers() -> None:
    stickers = [
        Sticker(emoji="fire", start=1, end=9),  # runs past the end: trimmed to 6
        Sticker(emoji="heart", start=1.5, end=3),  # same spot, same time: dropped
        Sticker(emoji="clap", start=5.8, end=7),  # only 0.2 s left: dropped
        *[
            Sticker(emoji="star", start=2, end=3, position=p)
            for p in ("top_left", "middle_left", "middle_right")
        ],
        Sticker(emoji="rocket", start=4, end=5, position="top_left"),
    ]
    fixed, errors = validate_plan(_plan(stickers=stickers), 60.0, get_preset("dynamic_reels"), set())
    assert errors == []
    assert fixed.stickers[0].emoji == "fire" and fixed.stickers[0].end == 6.0
    assert len(fixed.stickers) == 4 and "heart" not in {s.emoji for s in fixed.stickers}


@pytest.mark.parametrize(("captions", "before", "after"), [
    ("top", "top_left", "middle_left"),
    ("middle", "middle_right", "top_right"),
    ("lower_third", "top_left", "top_left"),
])  # fmt: skip
def test_stickers_leave_the_caption_band(captions: str, before: str, after: str) -> None:
    plan = _plan(
        captions=Captions(position=captions),
        stickers=[Sticker(emoji="fire", start=1, end=2, position=before)],
    )
    forced = force_job_settings(
        plan, aspect="9:16", style_preset="dynamic_reels", is_trial=False, has_words=True
    )
    assert forced.stickers[0].position == after


def test_a_sticker_gets_a_pop() -> None:
    plan = _plan(sfx=Sfx(enabled=True), stickers=[Sticker(emoji="fire", start=2, end=3)])
    assert [(c.kind, c.t) for c in plan_cues(plan)] == [("pop", 2.0)]


# ---------- graph ----------


def test_graph_order_is_look_then_stickers_then_text(tmp_path: Path) -> None:
    st = [PlacedSticker(ASSETS / "emoji" / "fire.png", 1.0, 2.5, "top_right", 172)]
    graph = _video_graph(tmp_path / "c.ass", tmp_path, tmp_path, "warm", stickers=st, first_sticker_input=2,
                         size=(1080, 1920))  # fmt: skip
    assert graph.startswith("[0:v]colorbalance=") and "[2:v]format=rgba" in graph
    assert graph.index("overlay=") < graph.index("ass=") and graph.endswith("[v]")
    assert "setpts=PTS+1.000/TB" in graph and "fade=t=out:st=1.300" in graph


def test_placement_sizes_by_width_and_skips_missing_images(tmp_path: Path) -> None:
    plan = _plan(stickers=[Sticker(emoji="fire", start=1, end=2, size_pct=20)])
    [placed] = place_stickers(plan, ASSETS, 1080)
    assert placed.size_px == 216 and placed.path.name == "fire.png"
    assert place_stickers(plan, tmp_path, 1080) == [] and place_stickers(plan, None, 1080) == []


# ---------- real ffmpeg ----------


def _pixel(video: Path, t: float, x: int, y: int) -> tuple[int, int, int]:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
         "-vf", f"crop=2:2:{x // 2 * 2}:{y // 2 * 2}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return raw[0], raw[1], raw[2]


async def test_a_sticker_appears_only_while_it_should(tmp_path: Path) -> None:
    joined = tmp_path / "grey.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x808080:s=360x640:r=30:d=4",
         "-f", "lavfi", "-i", "sine=frequency=300:d=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest",
         str(joined)],
        check=True,
    )  # fmt: skip
    plan = make_plan(
        [(0, 4)],
        captions=Captions(enabled=False),
        stickers=[Sticker(emoji="heart", start=1.0, end=3.0, position="middle_left", size_pct=30)],
    )
    out = tmp_path / "final.mp4"
    await render_final(joined=joined, ass_path=None, music_path=None, plan=plan, target_w=360, target_h=640,
                       out_path=out, fonts_dir=tmp_path / "f", assets_dir=ASSETS)  # fmt: skip
    assert (await probe(str(out))).duration_sec == pytest.approx(4.0, abs=0.2)
    cx, cy = round(0.06 * 360 + 54), round(0.45 * 640)  # centre of the 108 px heart
    grey = _pixel(out, 0.5, cx, cy)
    red = _pixel(out, 2.0, cx, cy)
    assert max(grey) - min(grey) < 12  # nothing there before the start
    assert red[0] > 150 and red[1] < 110  # the red heart is there
    after = _pixel(out, 3.6, cx, cy)
    assert max(after) - min(after) < 12  # and gone after the end
