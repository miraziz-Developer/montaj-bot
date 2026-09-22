import re

import pytest

from app.schemas.edit_plan import Captions, TextOverlay, Watermark
from app.services.render.captions_ass import (
    ass_color,
    ass_time,
    build_ass,
    build_timeline,
    has_events,
)
from app.services.render.text_safety import escape_ass
from tests.fakes.gemini import make_plan, make_transcript

W, H = 1080, 1920


def _ass(plan, transcript):  # noqa: ANN001, ANN202
    return build_ass(plan, transcript, build_timeline(plan.clips), target_w=W, target_h=H)


def _events(ass: str) -> list[str]:
    return [line for line in ass.splitlines() if line.startswith("Dialogue:")]


def _fields(line: str) -> list[str]:
    return line.split(",", 9)  # Layer, Start, End, Style, Name, ML, MR, MV, Effect, Text


# ---------- escape_ass ----------


def test_injection_is_neutralised() -> None:
    result = escape_ass(r"{\pos(0,0)}Malicious\Ntext")
    assert "{" not in result and "}" not in result and "\\" not in result
    assert result == "/pos(0,0)Malicious/Ntext"  # the backslash became "/", so this "N" is no line break


def test_control_characters_and_line_separators_cannot_end_an_event() -> None:
    assert escape_ass("a\nb\r\nc\x00d\x07e f g\x85h") == "a b c d e f g h"
    assert escape_ass("  many   spaces \t here ") == "many spaces here"
    assert escape_ass("") == "" and escape_ass("{}") == ""


def test_escape_keeps_uzbek_text_intact_and_is_idempotent() -> None:
    text = "O‘zbekiston, g‘alaba! Ta’lim — 100%"
    assert escape_ass(text) == text
    assert escape_ass(escape_ass("{a}\\b")) == escape_ass("{a}\\b")


# ---------- colours and times ----------


def test_colour_and_time_conversion() -> None:
    assert ass_color("#FFD400") == "&H0000D4FF"  # RRGGBB -> AABBGGRR
    assert ass_color("#102030") == "&H00302010"
    assert ass_color("#FFFFFF", 0x80) == "&H80FFFFFF"
    assert [ass_time(t) for t in (0, 61.256, 3661.5, 0.004)] == [
        "0:00:00.00",
        "0:01:01.26",
        "1:01:01.50",
        "0:00:00.00",
    ]


# ---------- header and styles ----------


def test_header_and_styles() -> None:
    plan = make_plan([(0, 4)], captions=Captions(position="lower_third", font_size_pct=5.0))
    ass = _ass(plan, make_transcript([]))
    for line in (
        "ScriptType: v4.00+",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
    ):
        assert line in ass
    assert (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding" in ass
    )
    styles = {
        line.split(":")[1].split(",")[0].strip(): line.split(",")
        for line in ass.splitlines()
        if line.startswith("Style:")
    }
    assert set(styles) == {"Cap", "Title", "Cta", "Lower", "Wm"}
    cap = styles[
        "Cap"
    ]  # Name,Font,Size,Pri,Sec,Out,Back,Bold,I,U,S,SX,SY,Sp,An,BS,Outline,Shadow,Align,ML,MR,MV,Enc
    assert cap[2] == "96" and cap[7] == "-1" and cap[15] == "1"  # 5 % of 1920, bold, BorderStyle 1
    assert (
        float(cap[16]) == pytest.approx(0.12 * 96, abs=0.1) and cap[17] == "0"
    )  # outline 0.12*size, no shadow
    assert (cap[18], cap[19], cap[21]) == ("2", "65", "346")  # lower_third: align 2, 0.06*W, 0.18*H
    assert (styles["Title"][18], styles["Cta"][18], styles["Lower"][18], styles["Wm"][18]) == (
        "8",
        "2",
        "1",
        "3",
    )
    assert styles["Cta"][21] == "576" and styles["Title"][2] == "115" and styles["Wm"][3] == "&H80FFFFFF"


@pytest.mark.parametrize(
    ("position", "align", "margin_v"), [("top", "8", "115"), ("middle", "5", "0"), ("bottom", "2", "115")]
)
def test_caption_position_maps_to_alignment_and_margin(position: str, align: str, margin_v: str) -> None:
    ass = _ass(make_plan([(0, 4)], captions=Captions(position=position)), make_transcript([]))  # type: ignore[arg-type]
    cap = next(line.split(",") for line in ass.splitlines() if line.startswith("Style: Cap"))
    assert (cap[18], cap[21]) == (align, margin_v)


# ---------- caption modes ----------

THREE = make_transcript([("bir", 1.0, 1.4), ("ikki", 1.5, 1.9), ("uch", 2.0, 2.5)])


def test_word_highlight_makes_one_event_per_word_showing_the_whole_group() -> None:
    plan = make_plan([(0, 4)], captions=Captions(style="word_highlight", max_words_per_line=3))
    events = _events(_ass(plan, THREE))
    assert len(events) == 3
    starts = [_fields(e)[1] for e in events]
    assert starts == ["0:00:01.00", "0:00:01.50", "0:00:02.00"]
    assert [_fields(e)[2] for e in events][:2] == starts[1:]  # each word runs until the next one starts
    assert _fields(events[2])[2] == "0:00:02.55"  # last word: its own end + 0.05
    hl, pri = ass_color("#FFD400"), ass_color("#FFFFFF")
    assert _fields(events[0])[9] == f"{{\\c{hl}&}}bir{{\\c{pri}&}} ikki uch"
    assert _fields(events[1])[9] == f"bir {{\\c{hl}&}}ikki{{\\c{pri}&}} uch"
    assert all(_fields(e)[3] == "Cap" for e in events)


def test_classic_makes_one_event_per_group() -> None:
    plan = make_plan([(0, 4)], captions=Captions(style="classic", max_words_per_line=3))
    [event] = _events(_ass(plan, THREE))
    fields = _fields(event)
    assert (fields[1], fields[2], fields[9]) == ("0:00:01.00", "0:00:02.50", "bir ikki uch")


def test_groups_split_on_max_words_and_on_long_pauses() -> None:
    transcript = make_transcript(
        [("a", 0.0, 0.3), ("b", 0.4, 0.7), ("c", 0.8, 1.1), ("d", 3.0, 3.3), ("e", 3.4, 3.7)], segment_gap=5
    )
    plan = make_plan([(0, 6)], captions=Captions(style="classic", max_words_per_line=2))
    assert [_fields(e)[9] for e in _events(_ass(plan, transcript))] == ["a b", "c", "d e"]
    plan = make_plan([(0, 6)], captions=Captions(style="classic", max_words_per_line=6))
    assert [_fields(e)[9] for e in _events(_ass(plan, transcript))] == ["a b c", "d e"]  # 1.9 s pause


def test_groups_never_cross_clip_boundaries() -> None:
    transcript = make_transcript([("a", 1.0, 1.3), ("b", 5.0, 5.3)], segment_gap=9)
    plan = make_plan([(0, 3), (4, 7)], captions=Captions(style="classic", max_words_per_line=6))
    assert [_fields(e)[9] for e in _events(_ass(plan, transcript))] == ["a", "b"]


def test_long_groups_are_split_over_two_lines_by_our_own_break() -> None:
    transcript = make_transcript([(f"w{i}", i * 0.4, i * 0.4 + 0.3) for i in range(5)], segment_gap=9)
    plan = make_plan([(0, 4)], captions=Captions(style="classic", max_words_per_line=6))
    [event] = _events(_ass(plan, transcript))
    assert _fields(event)[9] == "w0 w1 w2\\Nw3 w4"


def test_uppercase_option() -> None:
    plan = make_plan([(0, 4)], captions=Captions(style="classic", uppercase=True))
    assert _fields(_events(_ass(plan, make_transcript([("o‘zbek", 1.0, 1.4)])))[0])[9] == "O‘ZBEK"


# ---------- timeline mapping ----------


def test_words_are_mapped_through_clip_offset_and_speed() -> None:
    plan = make_plan([(0, 4), (10, 14)], captions=Captions(style="classic"))
    plan.clips[1].speed = 2.0  # clip 2 starts at output 4.0 and lasts 2 s
    transcript = make_transcript([("x", 11.0, 11.4)], segment_gap=9)
    [event] = _events(_ass(plan, transcript))
    fields = _fields(event)
    assert (fields[1], fields[2]) == ("0:00:04.50", "0:00:04.70")  # 4 + (11-10)/2 .. 4 + (11.4-10)/2


def test_words_outside_the_clips_are_skipped_and_edge_words_clamped() -> None:
    transcript = make_transcript(
        [("before", 0.5, 0.9), ("edge", 2.97, 3.4), ("inside", 3.5, 3.9), ("after", 8.0, 8.4)], segment_gap=9
    )
    plan = make_plan([(3.0, 6.0)], captions=Captions(style="classic", max_words_per_line=1))
    events = _events(_ass(plan, transcript))
    assert [_fields(e)[9] for e in events] == ["edge", "inside"]  # 2.97 is within 0.05 of the clip start
    assert _fields(events[0])[1] == "0:00:00.00"  # clamped to the clip start


def test_captions_disabled_produces_no_caption_events() -> None:
    plan = make_plan([(0, 4)], captions=Captions(enabled=False))
    assert not has_events(_ass(plan, THREE))


# ---------- overlays and watermark ----------


def test_overlays_use_their_own_times_and_styles() -> None:
    plan = make_plan(
        [(0, 10)],
        captions=Captions(enabled=False),
        overlays=[
            TextOverlay(text="Yangi Malibu", start=0.5, end=2.5, style="title"),
            TextOverlay(text="Bizga qo‘ng‘iroq qiling", start=8.0, end=99.0, style="cta"),
            TextOverlay(text="Toshkent", start=1.0, end=3.0, style="lower_third"),
            TextOverlay(text="Kech", start=12.0, end=13.0),
        ],
    )
    events = _events(_ass(plan, make_transcript([])))
    assert [(_fields(e)[3], _fields(e)[1], _fields(e)[2]) for e in events] == [
        ("Title", "0:00:00.50", "0:00:02.50"),
        ("Cta", "0:00:08.00", "0:00:10.00"),  # capped at the video length
        ("Lower", "0:00:01.00", "0:00:03.00"),
    ]  # the overlay starting after the end is dropped
    assert _fields(events[0])[9] == "{\\an8}Yangi Malibu"


def test_title_position_changes_its_alignment() -> None:
    plan = make_plan(
        [(0, 4)],
        captions=Captions(enabled=False),
        overlays=[TextOverlay(text="T", start=0, end=2, position="middle")],
    )
    assert _fields(_events(_ass(plan, make_transcript([])))[0])[9] == "{\\an5}T"


def test_watermark_spans_the_whole_video() -> None:
    plan = make_plan(
        [(0, 4), (5, 8)],
        captions=Captions(enabled=False),
        watermark=Watermark(enabled=True, text="@video_editor_uzbot"),
    )
    [event] = _events(_ass(plan, make_transcript([])))
    fields = _fields(event)
    assert (fields[3], fields[1], fields[2], fields[9]) == (
        "Wm",
        "0:00:00.00",
        "0:00:07.00",
        "@video_editor_uzbot",
    )
    assert not has_events(
        _ass(
            make_plan([(0, 4)], captions=Captions(enabled=False), watermark=Watermark(enabled=True, text="")),
            make_transcript([]),
        )
    )
    assert not has_events(_ass(make_plan([(0, 4)], captions=Captions(enabled=False)), make_transcript([])))


# ---------- security ----------

OUR_OVERRIDES = re.compile(r"\{\\(?:c&H[0-9A-F]{8}&|an[258])\}")


def test_hostile_text_never_reaches_the_ass_file_as_markup() -> None:
    hostile = r"{\pos(0,0)\fs500}Evil\Nline{\p1}m 0 0 l 99 99{\p0}"
    transcript = make_transcript([(hostile, 1.0, 1.4), ("x\\Nhack\ny", 1.5, 1.9)])
    plan = make_plan(
        [(0, 6)],
        captions=Captions(style="word_highlight"),
        overlays=[TextOverlay(text=hostile[:79], start=0, end=3)],
        watermark=Watermark(enabled=True, text=hostile[:40]),
    )
    for event in _events(_ass(plan, transcript)):
        text = _fields(event)[9]
        leftover = OUR_OVERRIDES.sub("", text)  # remove the override blocks WE generate
        assert "{" not in leftover and "}" not in leftover, text
        assert "\\pos" not in text and "\\fs" not in text and "\\p1" not in text
        assert leftover.replace("\\N", "").count("\\") == 0, text  # only our own \N line breaks remain
    assert (
        len(_events(_ass(plan, transcript))) == 4
    )  # 2 caption words + overlay + watermark, no extra lines injected
