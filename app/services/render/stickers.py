"""Emoji stickers: Noto Emoji PNGs (assets/emoji, Apache-2.0) composited with ffmpeg `overlay`.

libass cannot draw colour emoji, so stickers are real images. The plan only names one of STICKERS; the
file, size, position and the bounce-in animation are all decided here (nothing from the AI reaches a filter
string except numbers we compute)."""

from dataclasses import dataclass
from pathlib import Path

STICKERS: tuple[str, ...] = (
    "fire", "heart", "heart_eyes", "laugh", "wow", "mind_blown", "clap", "thumbs_up", "hundred", "star",
    "sparkles", "rocket", "money", "eyes", "check", "cross", "warning", "point_down", "point_right", "party",
    "gift", "idea", "muscle", "pray", "cool", "thinking", "sad", "angry", "trophy", "chart_up", "phone",
    "car", "food", "music", "bell", "lightning",
)  # fmt: skip

# centre of the sticker as a fraction of the frame; clear of the top-centre title and the caption band
_CENTRES = {
    "top_left": (None, 0.20),
    "top_right": (None, 0.20),
    "middle_left": (None, 0.45),
    "middle_right": (None, 0.45),
}
MARGIN = 0.06  # of the frame width
POP_IN_SEC = 0.12  # grows to POP_OVERSHOOT...
POP_SETTLE_SEC = 0.22  # ...and settles to 1.0
POP_OVERSHOOT = 1.15
FADE_OUT_SEC = 0.2


@dataclass(frozen=True, slots=True)
class PlacedSticker:
    path: Path
    start: float
    end: float
    position: str
    size_px: int


def sticker_file(assets_dir: Path, name: str) -> Path | None:
    path = assets_dir / "emoji" / f"{name}.png"
    return path if name in STICKERS and path.is_file() else None


def _centre(position: str, size: int, w: int, h: int) -> tuple[float, float]:
    _, fy = _CENTRES[position]
    margin = MARGIN * w + size / 2
    return (margin if position.endswith("left") else w - margin), fy * h


def sticker_graph(
    stickers: list[PlacedSticker], first_input: int, base: str, out: str, w: int, h: int
) -> str:
    """Graph text overlaying every sticker (input `first_input + i`) onto label `base`, producing `out`.
    Each image stream lasts end-start seconds and is shifted to `start`, so it is only processed while shown;
    its size follows a bounce curve (small -> overshoot -> 1.0) and it fades out at the end."""
    if not stickers:
        return f"[{base}]null[{out}]"
    parts, label = [], base
    for i, st in enumerate(stickers):
        size, dur = st.size_px, st.end - st.start
        grow = (
            f"if(lt(t,{POP_IN_SEC}),0.35+{POP_OVERSHOOT - 0.35:.2f}*t/{POP_IN_SEC},"
            f"if(lt(t,{POP_SETTLE_SEC}),{POP_OVERSHOOT}-{POP_OVERSHOOT - 1:.2f}*(t-{POP_IN_SEC})/"
            f"{POP_SETTLE_SEC - POP_IN_SEC:.2f},1))"
        )
        fade_at = max(0.0, dur - FADE_OUT_SEC)
        cx, cy = _centre(st.position, size, w, h)
        nxt = out if i == len(stickers) - 1 else f"st{i}"
        parts.append(
            f"[{first_input + i}:v]format=rgba,scale=w='{size}*{grow}':h='{size}*{grow}':eval=frame,"
            f"fade=t=out:st={fade_at:.3f}:d={FADE_OUT_SEC}:alpha=1,setpts=PTS+{st.start:.3f}/TB[sk{i}];"
            f"[{label}][sk{i}]overlay=x='{cx:.1f}-w/2':y='{cy:.1f}-h/2':eof_action=pass:format=auto[{nxt}]"
        )
        label = nxt
    return ";".join(parts)


def sticker_inputs(stickers: list[PlacedSticker], fps: int) -> list[str]:
    args: list[str] = []
    for st in stickers:
        args += ["-loop", "1", "-framerate", str(fps), "-t", f"{st.end - st.start:.3f}", "-i", str(st.path)]
    return args
