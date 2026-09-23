"""Render stage B: join the per-clip files (docs/EDIT_PLAN_SCHEMA.md section 6, `transition_in`).

Plain cuts (the common case) use the concat DEMUXER with stream copy - fast, no re-encode, valid because
stage A gives every clip identical codec/resolution/fps/audio parameters. As soon as ANY clip asks for a
real transition (crossfade / fade_black with a positive duration), the whole join switches to a
filter_complex chain of `xfade` (video) + `acrossfade` (audio) - there is no stream-copy equivalent of a
crossfade, so this path re-encodes. Actual rendered clip durations are probed (not read off the plan) so
`xfade`'s `offset` lines up exactly with what is really on disk.
"""

from collections.abc import Sequence
from pathlib import Path

from app.schemas.edit_plan import Clip
from app.services.media.ffmpeg import run_ffmpeg
from app.services.media.probe import media_duration

# "crossfade" rotates through a few tasteful, subtle built-in xfade transitions for visual variety across
# a video's cuts (a code decision, like zoom_levels/crossfade_sec - the schema only has one "crossfade"
# value, this doesn't add a new one). "fade_black" stays a single, deliberate effect - it is a distinct
# visual choice (a hard beat/scene break), not "crossfade with variety".
_CROSSFADE_ROTATION = ["fade", "dissolve", "smoothleft", "smoothright", "circleopen", "zoomin"]
_FIXED_TRANSITION = {"fade_black": "fadeblack"}


def _xfade_transition_name(clip_type: str, index: int) -> str:
    if clip_type == "crossfade":
        return _CROSSFADE_ROTATION[index % len(_CROSSFADE_ROTATION)]
    return _FIXED_TRANSITION[clip_type]


def quote_concat_path(path: Path) -> str:
    """Concat-demuxer quoting: wrap in single quotes; an embedded ' becomes '\\''."""
    return "'" + str(path.resolve()).replace("'", "'\\''") + "'"


def concat_list_text(clip_paths: list[Path]) -> str:
    return "".join(f"file {quote_concat_path(p)}\n" for p in clip_paths)


def wants_transition(clip: Clip) -> bool:
    return (
        clip.transition_in.type == "crossfade" or clip.transition_in.type in _FIXED_TRANSITION
    ) and clip.transition_in.duration > 0


async def concat_clips(
    clip_paths: list[Path],
    *,
    out_path: Path,
    workdir: Path,
    clips: Sequence[Clip] | None = None,
    timeout: float = 1800,
) -> None:
    """All clips share codec/resolution/fps/audio parameters (stage A). `clips` (same order as
    `clip_paths`) supplies each clip's `transition_in`; omit it (or pass an all-"cut" plan) to keep the
    fast stream-copy join - `clips[0].transition_in` is never consulted (nothing precedes the first clip).
    """
    if not clip_paths:
        raise ValueError("nothing to concatenate")
    if not clips or not any(wants_transition(c) for c in clips[1:]):
        list_file = workdir / "list.txt"
        list_file.write_text(concat_list_text(clip_paths), encoding="utf-8")
        args = [
            "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list_file.name,
            "-c", "copy", str(out_path),
        ]  # fmt: skip
        await run_ffmpeg(args, timeout=timeout, cwd=workdir)
        return
    await _concat_with_transitions(clip_paths, clips, out_path=out_path, workdir=workdir, timeout=timeout)


def build_transition_filter_complex(
    durations: Sequence[float], clips: Sequence[Clip]
) -> tuple[str, str, str]:
    """The `-filter_complex` string plus the final `[video]`/`[audio]` pad labels, given each clip's
    ALREADY-RENDERED duration (`durations`, same order as `clips`). Pure function so the offset/duration
    math can be unit-tested without invoking ffmpeg."""
    filters: list[str] = []
    v_label, a_label = "0:v", "0:a"
    timeline = durations[0]
    for i in range(1, len(clips)):
        clip = clips[i]
        nv, na = f"v{i}", f"a{i}"
        if wants_transition(clip):
            xfade_d = min(clip.transition_in.duration, durations[i - 1], durations[i])
            offset = max(timeline - xfade_d, 0.0)
            transition = _xfade_transition_name(clip.transition_in.type, i)
            filters.append(
                f"[{v_label}][{i}:v]xfade=transition={transition}:duration={xfade_d:.3f}:"
                f"offset={offset:.3f},format=yuv420p[{nv}]"
            )
            filters.append(f"[{a_label}][{i}:a]acrossfade=d={xfade_d:.3f}[{na}]")
            timeline = timeline - xfade_d + durations[i]
        else:
            filters.append(f"[{v_label}][{a_label}][{i}:v][{i}:a]concat=n=2:v=1:a=1[{nv}][{na}]")
            timeline += durations[i]
        v_label, a_label = nv, na
    return ";".join(filters), v_label, a_label


async def _concat_with_transitions(
    clip_paths: list[Path],
    clips: Sequence[Clip],
    *,
    out_path: Path,
    workdir: Path,
    timeout: float,
) -> None:
    durations = [await media_duration(p) for p in clip_paths]
    filter_complex, v_label, a_label = build_transition_filter_complex(durations, clips)

    inputs: list[str] = []
    for path in clip_paths:
        inputs += ["-i", str(path)]
    args = (
        ["-y", "-loglevel", "error", *inputs, "-filter_complex", filter_complex]
        + ["-map", f"[{v_label}]", "-map", f"[{a_label}]"]
        + [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            str(out_path),
        ]
    )  # fmt: skip
    await run_ffmpeg(args, timeout=timeout, cwd=workdir)
