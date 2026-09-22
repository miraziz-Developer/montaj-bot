"""ASS subtitles: captions, overlays and watermark (docs/EDIT_PLAN_SCHEMA.md sections 7 and 8)."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from app.schemas.edit_plan import Clip, EditPlan
from app.services.render.text_safety import escape_ass
from app.services.stt.base import Transcript

WORD_TOLERANCE_SEC = 0.05
MAX_PAUSE_SEC = 0.6  # a longer pause starts a new caption group
LAST_WORD_TAIL_SEC = 0.05
LONG_GROUP_WORDS = 5  # groups this long are split over two lines (WrapStyle 2 never wraps by itself)

_STYLE_FORMAT = (
    "Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
    "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
    "MarginL, MarginR, MarginV, Encoding"
)
_EVENT_FORMAT = "Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
_OVERLAY_STYLES = {"title": "Title", "cta": "Cta", "lower_third": "Lower"}
_TITLE_ALIGNMENT = {"top": 8, "middle": 5, "bottom": 2}


@dataclass(frozen=True, slots=True)
class ClipTimelineEntry:
    clip: Clip
    output_start: float  # seconds on the OUTPUT timeline where this clip begins


def build_timeline(clips: Sequence[Clip]) -> list[ClipTimelineEntry]:
    entries, offset = [], 0.0
    for clip in clips:
        entries.append(ClipTimelineEntry(clip, offset))
        offset += clip.out_duration
    return entries


def ass_color(rgb_hex: str, alpha: int = 0) -> str:
    """`#RRGGBB` -> `&HAABBGGRR`."""
    r, g, b = (int(rgb_hex[i : i + 2], 16) for i in (1, 3, 5))
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def ass_time(seconds: float) -> str:
    """`H:MM:SS.cc` (centiseconds)."""
    total_cs = max(0, round(seconds * 100))
    hours, rest = divmod(total_cs, 360000)
    minutes, rest = divmod(rest, 6000)
    secs, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def has_events(ass: str) -> bool:
    return "\nDialogue:" in ass


def _style(
    name: str, font: str, size: float, primary: str, outline: str, alignment: int,
    margin_lr: int, margin_v: int, *, outline_width: float | None = None, back: str = "&H00000000",
) -> str:  # fmt: skip
    width = 0.12 * size if outline_width is None else outline_width
    return (
        f"Style: {name},{font},{round(size)},{primary},{primary},{outline},{back},-1,0,0,0,100,100,0,0,1,"
        f"{width:.1f},0,{alignment},{margin_lr},{margin_lr},{margin_v},1"
    )


def _styles(plan: EditPlan, w: int, h: int) -> list[str]:
    caps, font = plan.captions, plan.captions.font
    cap_size = caps.font_size_pct / 100 * h
    position = {
        "top": (8, round(0.06 * h)),
        "middle": (5, 0),
        "lower_third": (2, round(0.18 * h)),
        "bottom": (2, round(0.06 * h)),
    }[caps.position]
    side = round(0.06 * w)
    white, black = ass_color("#FFFFFF"), ass_color("#000000")
    return [
        _style("Cap", font, cap_size, ass_color(caps.primary_color), ass_color(caps.outline_color),
               position[0], side, position[1]),
        _style("Title", font, 0.06 * h, white, black, 8, side, round(0.08 * h)),
        _style("Cta", font, 0.055 * h, ass_color(caps.highlight_color), black, 2, side, round(0.30 * h)),
        _style("Lower", font, 0.04 * h, white, black, 1, side, round(0.10 * h)),
        _style("Wm", font, 0.03 * h, ass_color("#FFFFFF", 0x80), ass_color("#000000", 0x80), 3,
               round(0.03 * w), round(0.03 * h)),
    ]  # fmt: skip


def _header(w: int, h: int, styles: list[str]) -> str:
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 2",
        "ScaledBorderAndShadow: yes", "", "[V4+ Styles]", f"Format: {_STYLE_FORMAT}", *styles, "",
        "[Events]", f"Format: {_EVENT_FORMAT}",
    ]  # fmt: skip
    return "\n".join(lines)


def _dialogue(start: float, end: float, style: str, text: str, layer: int = 0) -> str:
    return f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{text}"


@dataclass(frozen=True, slots=True)
class _Word:
    text: str  # already escaped
    start: float  # output timeline
    end: float


def _join(tokens: list[str]) -> str:
    """Words separated by spaces; long groups get one explicit line break (ours, never from input)."""
    if len(tokens) >= LONG_GROUP_WORDS:
        half = math.ceil(len(tokens) / 2)
        return " ".join(tokens[:half]) + "\\N" + " ".join(tokens[half:])
    return " ".join(tokens)


def _clip_words(entry: ClipTimelineEntry, transcript: Transcript, uppercase: bool) -> list[_Word]:
    clip = entry.clip

    def to_out(t: float) -> float:
        return entry.output_start + (min(max(t, clip.src_in), clip.src_out) - clip.src_in) / clip.speed

    words = []
    for word in transcript.all_words():
        if word.start >= clip.src_in - WORD_TOLERANCE_SEC and word.end <= clip.src_out + WORD_TOLERANCE_SEC:
            text = escape_ass(word.text.upper() if uppercase else word.text)
            if text:
                words.append(_Word(text, to_out(word.start), to_out(word.end)))
    return words


def _groups(words: list[_Word], max_words: int) -> list[list[_Word]]:
    groups: list[list[_Word]] = []
    current: list[_Word] = []
    for word in words:
        if current and (len(current) >= max_words or word.start - current[-1].end > MAX_PAUSE_SEC):
            groups.append(current)
            current = []
        current.append(word)
    if current:
        groups.append(current)
    return groups


def _caption_events(
    plan: EditPlan, transcript: Transcript, timeline: Sequence[ClipTimelineEntry]
) -> list[str]:
    caps = plan.captions
    highlight, primary = ass_color(caps.highlight_color), ass_color(caps.primary_color)
    events: list[str] = []
    for entry in timeline:
        for group in _groups(_clip_words(entry, transcript, caps.uppercase), caps.max_words_per_line):
            if caps.style == "classic":
                events.append(_dialogue(group[0].start, group[-1].end, "Cap", _join([w.text for w in group])))
                continue
            for i, word in enumerate(group):
                end = group[i + 1].start if i < len(group) - 1 else word.end + LAST_WORD_TAIL_SEC
                tokens = [
                    f"{{\\c{highlight}&}}{w.text}{{\\c{primary}&}}" if j == i else w.text
                    for j, w in enumerate(group)
                ]
                events.append(_dialogue(word.start, max(end, word.start + 0.02), "Cap", _join(tokens)))
    return events


def build_ass(
    plan: EditPlan,
    transcript: Transcript,
    timeline: Sequence[ClipTimelineEntry],
    *,
    target_w: int,
    target_h: int,
) -> str:
    """The complete .ass text. All user-influenced text is escaped with `escape_ass` first."""
    events: list[str] = []
    if plan.captions.enabled:
        events += _caption_events(plan, transcript, timeline)

    total = sum(entry.clip.out_duration for entry in timeline)
    for overlay in plan.overlays:
        text = escape_ass(overlay.text)
        if not text or overlay.start >= total:
            continue
        if overlay.style == "title":  # `position` moves only the title; cta/lower_third have fixed places
            text = f"{{\\an{_TITLE_ALIGNMENT[overlay.position]}}}{text}"
        events.append(
            _dialogue(overlay.start, min(overlay.end, total), _OVERLAY_STYLES[overlay.style], text, 1)
        )

    if plan.watermark.enabled and (mark := escape_ass(plan.watermark.text)):
        events.append(_dialogue(0.0, total, "Wm", mark, 2))

    header = _header(target_w, target_h, _styles(plan, target_w, target_h))
    return header + "\n" + "\n".join(events) + ("\n" if events else "")
