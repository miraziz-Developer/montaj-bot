"""Render stage C: captions/overlays/watermark burn-in, music mix, loudness, final encode."""

import logging
import re
from pathlib import Path

from app.schemas.edit_plan import EditPlan
from app.services.media.ffmpeg import FFmpegError, run_ffmpeg
from app.services.render.looks import look_filter
from app.services.render.sfx import SfxCue, plan_cues, sfx_graph

logger = logging.getLogger(__name__)

LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"
# after the effects are added on top of the normalised voice: catch their peaks (-1.5 dBFS), no auto-gain
SFX_LIMITER = "alimiter=limit=0.84:level=0"
# Substrings of ffmpeg errors that mean "this ffmpeg build lacks a filter/option we use for the music mix".
_UNSUPPORTED_HINTS = ("sidechaincompress", "normalize", "No such filter", "Option not found")


def escape_filter_value(value: str) -> str:
    """Escape a PATH for use as a filter option value. Only our own paths go through here, never user text."""
    for char in "\\:',[];":
        value = value.replace(char, "\\" + char)
    return value


def _relative(path: Path, cwd: Path) -> str:
    try:
        return str(path.resolve().relative_to(cwd.resolve()))
    except ValueError:
        return str(path.resolve())


def _video_graph(ass_path: Path | None, fonts_dir: Path, cwd: Path, look: str = "natural") -> str:
    grade = look_filter(look)
    if ass_path is None:
        return f"[0:v]{grade or 'null'}[v]"
    options = f"filename={escape_filter_value(_relative(ass_path, cwd))}"
    if fonts_dir.is_dir():
        options += f":fontsdir={escape_filter_value(_relative(fonts_dir, cwd))}"
    return f"[0:v]{grade + ',' if grade else ''}ass={options}[v]"


SILENCE_MAX_VOLUME_DB = -70.0
_MAX_VOLUME = re.compile(r"max_volume: (-?inf|-?[\d.]+) dB")


async def audio_is_silent(path: Path, timeout: float = 600) -> bool:
    """True if the audio track is (near) digital silence.

    `loudnorm` on pure silence yields NaN/-inf and makes the AAC encoder fail, so such audio must skip it.
    """
    _, _, stderr = await run_ffmpeg(
        ["-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"], timeout=timeout
    )
    match = _MAX_VOLUME.search(stderr)
    return match is None or float(match.group(1)) < SILENCE_MAX_VOLUME_DB


def _loudness(plan: EditPlan, *, skip_loudnorm: bool = False) -> str:
    chain = ["afftdn=nf=-25"] if plan.export.denoise_audio and not skip_loudnorm else []
    if plan.export.loudnorm and not skip_loudnorm:
        chain.append(LOUDNORM)
    return ",".join(chain) or "anull"


def _audio_graph(
    plan: EditPlan,
    music: bool,
    *,
    ducking: bool,
    normalize_off: bool,
    silent: bool = False,
    cues: list[SfxCue] | None = None,
) -> str:
    if cues:
        # effects go on top of the NORMALISED voice/music, so their level relative to speech is fixed
        # (calibrated in sfx.py) whatever the raw recording's loudness; a limiter catches the peaks
        base = _audio_graph(plan, music, ducking=ducking, normalize_off=normalize_off, silent=silent)
        fx = sfx_graph(cues, plan.sfx.volume, "voice", "mixfx")
        return f"{base.removesuffix('[a]')}[voice];{fx};[mixfx]{SFX_LIMITER}[a]"
    if not music:
        return f"[0:a]{_loudness(plan, skip_loudnorm=silent)}[a]"
    total = plan.total_duration()
    fade = min(plan.music.fade_out_sec, total)
    music_chain = f"volume={plan.music.volume:g}"
    if fade > 0:
        music_chain += f",afade=t=out:st={max(total - fade, 0):.3f}:d={fade:g}"
    amix = "amix=inputs=2:duration=first:dropout_transition=0" + (":normalize=0" if normalize_off else "")
    compensate = "" if normalize_off else ",volume=2"  # amix without normalize=0 halves the level
    if ducking:
        mix = (
            f"[1:a]{music_chain}[m];[0:a]asplit=2[vo1][vo2];"
            f"[m][vo1]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300[md];"
            f"[md][vo2]{amix}{compensate}[mix]"
        )
    else:
        mix = f"[1:a]{music_chain}[m];[m][0:a]{amix}{compensate}[mix]"
    return f"{mix};[mix]{_loudness(plan)}[a]"


async def render_final(
    *,
    joined: Path,
    ass_path: Path | None,
    music_path: Path | None,
    plan: EditPlan,
    target_w: int,
    target_h: int,
    out_path: Path,
    fonts_dir: Path,
    timeout: float = 3600,
) -> None:
    """Burn subtitles in (when there is an ASS file), mix music (ducked under speech), normalise loudness."""
    cwd = (ass_path or out_path).parent
    video = _video_graph(ass_path, fonts_dir, cwd, plan.look)
    cues = plan_cues(plan)
    music = music_path is not None and plan.music.enabled
    silent = not music and await audio_is_silent(joined)  # a music mix always has signal
    if silent:
        logger.info("joined audio is silent: skipping loudness normalisation")

    async def encode(*, ducking: bool, normalize_off: bool) -> None:
        audio = _audio_graph(
            plan, music, ducking=ducking, normalize_off=normalize_off, silent=silent, cues=cues
        )
        graph = f"{video};{audio}"
        args = ["-y", "-loglevel", "error", "-i", str(joined)]
        if music:
            args += ["-stream_loop", "-1", "-i", str(music_path)]
        args += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]"]
        args += [
            "-c:v", "libx264", "-preset", plan.export.preset, "-crf", str(plan.export.crf),
            "-pix_fmt", "yuv420p", "-r", str(plan.target.fps),
            "-c:a", "aac", "-b:a", f"{plan.export.audio_bitrate_k}k", "-movflags", "+faststart",
        ]  # fmt: skip
        if music:  # the looped music must end with the video
            args += ["-t", f"{plan.total_duration():.3f}"]
        args.append(str(out_path))
        await run_ffmpeg(args, timeout=timeout, cwd=cwd)

    if not music:
        await encode(ducking=False, normalize_off=False)
        return
    # VERIFY: `normalize=0` needs ffmpeg >= 4.4. Fallbacks: no ducking, then plain amix (level compensated).
    attempts = [(plan.music.ducking, True)]
    if plan.music.ducking:
        attempts.append((False, True))
    attempts.append((False, False))
    for index, (ducking, normalize_off) in enumerate(attempts):
        try:
            await encode(ducking=ducking, normalize_off=normalize_off)
            return
        except FFmpegError as exc:
            if index == len(attempts) - 1 or not any(hint in str(exc) for hint in _UNSUPPORTED_HINTS):
                raise
            logger.warning(
                "ffmpeg rejected the music mix (ducking=%s normalize_off=%s): falling back",
                ducking,
                normalize_off,
            )
