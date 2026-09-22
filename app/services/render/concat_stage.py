"""Render stage B: join the per-clip files with the concat demuxer (`-c copy`)."""

from pathlib import Path

from app.services.media.ffmpeg import run_ffmpeg


def quote_concat_path(path: Path) -> str:
    """Concat-demuxer quoting: wrap in single quotes; an embedded ' becomes '\\''."""
    return "'" + str(path.resolve()).replace("'", "'\\''") + "'"


def concat_list_text(clip_paths: list[Path]) -> str:
    return "".join(f"file {quote_concat_path(p)}\n" for p in clip_paths)


async def concat_clips(
    clip_paths: list[Path], *, out_path: Path, workdir: Path, timeout: float = 1800
) -> None:
    """All clips share codec/resolution/fps/audio parameters (stage A), so stream copy is valid."""
    if not clip_paths:
        raise ValueError("nothing to concatenate")
    list_file = workdir / "list.txt"
    list_file.write_text(concat_list_text(clip_paths), encoding="utf-8")
    args = [
        "-y",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_file.name,
        "-c",
        "copy",
        str(out_path),
    ]
    await run_ffmpeg(args, timeout=timeout, cwd=workdir)
