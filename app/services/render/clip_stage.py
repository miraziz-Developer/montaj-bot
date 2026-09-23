"""Render stage A: one re-encoded file per clip (docs/EDIT_PLAN_SCHEMA.md section 6)."""

import logging
from pathlib import Path

from app.schemas.edit_plan import Clip, Reframe
from app.services.media.ffmpeg import run_ffmpeg
from app.services.media.probe import probe

logger = logging.getLogger(__name__)


ZOOM_PUSH_IN = 0.08  # Ken Burns: a gentle 8% push-in over the clip's duration (static shots feel less flat)
MIN_ANIMATE_SEC = 0.4  # clips shorter than this keep a plain static crop - too brief for motion to read


def _even(value: float) -> int:
    n = int(round(value))
    return n if n % 2 == 0 else n + 1


def fill_chain(target_w: int, target_h: int, reframe: Reframe, *, duration: float = 0.0) -> str:
    """Cover the target at zoom z, then crop around the focus point.

    Every number is computed here in Python; only ffmpeg's own `min`/`max`/`t` expression functions appear
    in the filter (consistent approach). The quotes keep the commas of the expressions out of the filter
    parser.

    When `duration` is at least MIN_ANIMATE_SEC, the crop window shrinks linearly over the clip - a slow,
    subtle "Ken Burns" push-in from `reframe.zoom` towards `reframe.zoom * (1 + ZOOM_PUSH_IN)` - instead of
    staying static. Pre-scaling covers the END (most zoomed-in, i.e. smallest-crop) state; crop's own `ow`/
    `oh` (its evaluated width/height) are reused in the x/y expressions so the animation is written once.
    A trailing `scale` normalizes the per-frame-varying crop size back to a constant output resolution.
    """
    animate = duration >= MIN_ANIMATE_SEC
    end_zoom = reframe.zoom * (1 + ZOOM_PUSH_IN) if animate else reframe.zoom
    scaled_w, scaled_h = _even(target_w * end_zoom), _even(target_h * end_zoom)

    if not animate:
        half_w, half_h = f"{target_w / 2:g}", f"{target_h / 2:g}"
        x = f"'min(max(iw*{reframe.focus_x:.4f}-{half_w},0),iw-{target_w})'"
        y = f"'min(max(ih*{reframe.focus_y:.4f}-{half_h},0),ih-{target_h})'"
        return (
            f"scale=w={scaled_w}:h={scaled_h}:force_original_aspect_ratio=increase:force_divisible_by=2,"
            f"crop={target_w}:{target_h}:x={x}:y={y}"
        )

    start_w, start_h = target_w * (1 + ZOOM_PUSH_IN), target_h * (1 + ZOOM_PUSH_IN)
    frac = f"min(t/{duration:.4f},1)"
    w = f"'min({start_w:g}-({start_w - target_w:g})*{frac},iw)'"
    h = f"'min({start_h:g}-({start_h - target_h:g})*{frac},ih)'"
    x = f"'min(max(iw*{reframe.focus_x:.4f}-ow/2,0),iw-ow)'"
    y = f"'min(max(ih*{reframe.focus_y:.4f}-oh/2,0),ih-oh)'"
    return (
        f"scale=w={scaled_w}:h={scaled_h}:force_original_aspect_ratio=increase:force_divisible_by=2,"
        f"crop={w}:{h}:x={x}:y={y},"
        f"scale={target_w}:{target_h}"
    )


def fit_blur_graph(target_w: int, target_h: int, speed: float, fps: int) -> str:
    """Whole frame visible over a blurred, cropped copy of itself. `W`,`H`,`w`,`h` are ffmpeg variables."""
    w, h = target_w, target_h
    return (
        f"[0:v]{color_polish()}[polished];"
        f"[polished]split=2[a][b];"
        f"[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:5[bg];"
        f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setpts=PTS/{speed:g},fps={fps},format=yuv420p[v]"
    )


def color_polish() -> str:
    """A gentle, universal contrast/saturation/sharpness lift - the kind of default "polish" pass consumer
    editing apps apply automatically. Deliberately conservative: no brightness change (would clip already-
    bright or already-dark footage unpredictably) and no CHROMA sharpening (avoids color fringing on edges).
    Applied to every clip regardless of reframe mode."""
    return "eq=contrast=1.06:saturation=1.12,unsharp=lx=5:ly=5:la=0.4:cx=5:cy=5:ca=0.0"


def stabilize() -> str:
    """Corrects small handheld shake (ffmpeg's `deshake`, default parameters). Only meaningful for real
    camera footage - screen recordings/video notes (reframe.mode "fit_blur") skip it, since deshake's
    motion model assumes real-world camera movement and can misbehave on on-screen UI motion. Placed FIRST
    in the chain (before scale/crop) so any edge artifact it introduces is cropped away by reframing,
    rather than visible at the final frame edge."""
    return "deshake"


def audio_filter(clip: Clip) -> str:
    parts = []
    if clip.speed != 1.0:
        parts.append(f"atempo={clip.speed:g}")  # one instance covers the schema's 0.5..2.0 range
    parts.append(f"volume={0 if clip.audio.mute else clip.audio.volume:g}")
    # `apad` pads with silence if the (possibly dubbed-in, P13) audio window decodes shorter than the
    # video: the trailing `-t {out_dur}` on the whole command still caps it, so this only ever adds
    # silence, never extra length.
    parts += ["aresample=48000", "aformat=channel_layouts=stereo", "apad"]
    return ",".join(parts)


async def render_clip(
    source: Path,
    clip: Clip,
    *,
    out_path: Path,
    target_w: int,
    target_h: int,
    fps: int,
    has_audio: bool | None = None,
    audio_duration_sec: float | None = None,
    audio_source: Path | None = None,
    preset: str = "veryfast",
    crf: int = 21,
    audio_bitrate_k: int = 160,
    timeout: float = 1800,
) -> None:
    """Cut [src_in, src_out] out of `source`, reframe to target_w x target_h, apply speed/volume, re-encode.

    Input seeking + re-encode keeps the cut frame-accurate. A source without audio - or a clip whose window
    starts at/after the SOURCE'S AUDIO STREAM'S OWN duration (some recordings have a shorter audio track
    than video track, e.g. the mic cutting out before the camera stops) - gets a silent stereo track
    (`anullsrc`) instead, so that every clip has identical streams and the join can use `-c copy` or xfade.

    P13 B-roll dub: when `clip.audio.source == "primary"`, `audio_source` (the job's PRIMARY file, always
    passed by the caller in that case) supplies the audio instead - trimmed to `clip.audio.primary_src_in/
    primary_src_out` - so the narration keeps playing under a muted-video B-roll cutaway. `has_audio`/
    `audio_duration_sec` are about `source`'s own track and are irrelevant (and skipped) in that case.
    """
    dub = audio_source is not None and clip.audio.source == "primary"
    if not dub and (has_audio is None or audio_duration_sec is None):
        info = await probe(str(source))
        has_audio = info.has_audio if has_audio is None else has_audio
        audio_duration_sec = info.audio_duration_sec if audio_duration_sec is None else audio_duration_sec
    if not dub and has_audio and audio_duration_sec is not None and clip.src_in >= audio_duration_sec:
        has_audio = False
    src_dur = clip.src_out - clip.src_in
    out_dur = clip.out_duration

    args = [
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{clip.src_in:.3f}",
        "-t",
        f"{src_dur:.3f}",
        "-i",
        str(source),
    ]
    if dub:
        a_in, a_out = clip.audio.primary_src_in or 0.0, clip.audio.primary_src_out or 0.0
        args += ["-ss", f"{a_in:.3f}", "-t", f"{a_out - a_in:.3f}", "-i", str(audio_source)]
        audio_map = "1:a:0"
    elif not has_audio:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        audio_map = "1:a:0"
    else:
        audio_map = "0:a:0"

    if clip.reframe.mode == "fit_blur":
        args += ["-filter_complex", fit_blur_graph(target_w, target_h, clip.speed, fps), "-map", "[v]"]
    else:
        chain = fill_chain(target_w, target_h, clip.reframe, duration=src_dur)
        args += [
            "-vf",
            f"{stabilize()},{color_polish()},{chain},setpts=PTS/{clip.speed:g},fps={fps},format=yuv420p",
            "-map", "0:v:0",
        ]  # fmt: skip
    args += ["-af", audio_filter(clip), "-map", audio_map, "-t", f"{out_dur:.3f}"]
    args += [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-profile:v", "high", "-g", str(2 * fps),
        "-c:a", "aac", "-b:a", f"{audio_bitrate_k}k", "-ar", "48000", "-ac", "2",
        str(out_path),
    ]  # fmt: skip
    await run_ffmpeg(args, timeout=timeout, cwd=out_path.parent)
