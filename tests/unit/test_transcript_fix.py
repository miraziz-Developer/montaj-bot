import subprocess
from pathlib import Path

import pytest

from app.core.errors import AIError
from app.services.ai.transcript_fix import align_corrected, correct_transcript
from app.services.stt.base import Transcript, Word
from tests.fakes.gemini import FakeGeminiClient, make_transcript

# what Azure heard in a real car-sale video, with its word times (continuous speech)
HEARD = [
    ("narxini", 33.28, 33.84), ("6300", 34.16, 34.88), ("dollar", 34.88, 35.12), ("qo'ydik.", 35.12, 35.68),
    ("Kimga", 35.68, 36.0), ("qizil", 36.0, 36.24), ("bo'lsa", 36.24, 36.4), ("nasya", 36.4, 36.9),
    ("sodaga", 36.9, 37.4), ("beramiz", 37.4, 38.0),
]  # fmt: skip


def _words(items=HEARD) -> list[Word]:  # noqa: ANN001
    return [Word(text=t, start=a, end=b) for t, a, b in items]


def _align(corrected: str, items=HEARD):  # noqa: ANN001, ANN202
    words = _words(items)
    return align_corrected(words, [0] * len(words), corrected)


def test_misheard_words_get_the_correct_text_on_the_original_timing() -> None:
    out = _align("narxini 6300 dollar qo‘ydik. Kimga qiziq bo‘lsa nasiya savdoga beramiz")
    assert out is not None
    assert [w.text for w in out] == [
        "narxini", "6300", "dollar", "qo‘ydik.", "Kimga", "qiziq", "bo‘lsa", "nasiya", "savdoga", "beramiz",
    ]  # fmt: skip
    by_text = {w.text: w for w in out}
    assert (by_text["qiziq"].start, by_text["qiziq"].end) == (36.0, 36.24)  # "qizil"'s slot
    assert by_text["nasiya"].start == 36.4 and by_text["savdoga"].end == pytest.approx(37.4)
    assert (by_text["6300"].start, by_text["6300"].end) == (34.16, 34.88)  # unchanged words keep their times


def test_apostrophe_style_alone_counts_as_the_same_word() -> None:
    out = _align("narxini 6300 dollar qo‘ydik. Kimga qizil bo‘lsa nasya sodaga beramiz")
    assert out is not None and out[3].text == "qo‘ydik." and (out[3].start, out[3].end) == (35.12, 35.68)


def test_a_merged_word_split_in_two_shares_the_original_slot() -> None:
    items = [("hanasi", 29.9, 30.6), ("shunaqa", 30.64, 31.0), ("zo'r", 31.04, 31.28)]
    out = _align("ha nasi shunaqa zo‘r", items)
    assert out is not None and [w.text for w in out[:2]] == ["ha", "nasi"]
    assert out[0].start == 29.9 and out[1].end == pytest.approx(30.6) and out[0].end == out[1].start


def test_a_wholly_different_text_is_rejected() -> None:
    assert _align("Assalomu alaykum hurmatli mijozlar bugun sizga ajoyib mashina taklif qilamiz") is None


def test_gemini_skipping_speech_keeps_what_the_stt_heard() -> None:
    out = _align("narxini 6300 dollar qo‘ydik. Kimga qiziq bo‘lsa")  # the last 3 words are missing
    assert out is not None and [w.text for w in out[-3:]] == ["nasya", "sodaga", "beramiz"]


def test_a_single_dropped_or_added_word_is_accepted_but_long_additions_are_not() -> None:
    dropped = _align("narxini 6300 dollar qo‘ydik. Kimga qizil bo‘lsa nasya beramiz")
    assert dropped is not None and "sodaga" not in [w.text for w in dropped]
    added = _align("narxini 6300 dollar qo‘ydik. Kimga qizil bo‘lsa nasya sodaga beramiz aka")
    assert added is not None and added[-1].text == "aka" and added[-1].end == pytest.approx(38.0)
    invented = _align(
        "narxini 6300 dollar qo‘ydik. Kimga qizil bo‘lsa nasya sodaga beramiz tez orada qo‘ng‘iroq qiling"
    )
    assert invented is not None and invented[-1].text == "beramiz"


def test_times_stay_ordered_and_inside_the_original_span() -> None:
    out = _align("narxi 6300 dollar qo‘ydik. Kimga qiziq bo‘lsa nasiya savdoga beramiz")
    assert out is not None
    assert all(a.end <= b.start + 1e-9 for a, b in zip(out, out[1:], strict=False))
    assert out[0].start >= 33.28 and out[-1].end <= 38.0


# ---------- the whole step (real ffmpeg audio, fake Gemini) ----------


@pytest.fixture(scope="module")
def proxy(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("fix") / "proxy.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=160x90:r=10:d=40",
         "-f", "lavfi", "-i", "sine=frequency=300:d=40", "-shortest", "-c:v", "libx264", "-c:a", "aac",
         str(path)],
        check=True,
    )  # fmt: skip
    return path


def _transcript() -> Transcript:
    return make_transcript([(t, a - 30, b - 30) for t, a, b in HEARD])  # 3.28 .. 8.0 s


async def test_correct_transcript_fixes_words_marks_it_and_keeps_segments(
    proxy: Path, tmp_path: Path
) -> None:
    llm = FakeGeminiClient()
    llm.transcript_fix = lambda draft: draft.replace("qizil", "qiziq").replace(
        "nasya sodaga", "nasiya savdoga"
    )
    fixed, usage = await correct_transcript(
        llm, _transcript(), proxy_path=proxy, duration_sec=40, workdir=tmp_path
    )
    assert fixed.corrected and usage.input_tokens == 50
    text = " ".join(w.text for w in fixed.all_words())
    assert "qiziq" in text and "nasiya savdoga" in text and "qizil" not in text
    assert len(fixed.segments) == len(_transcript().segments)
    assert not list(tmp_path.glob("*.ogg"))  # temp audio removed


async def test_failures_and_repeats_keep_the_transcript(proxy: Path, tmp_path: Path) -> None:
    original = _transcript()

    class Broken(FakeGeminiClient):
        async def correct_transcript(self, *, audio_path: Path, draft: str):  # noqa: ANN202
            raise AIError("boom")

    fixed, _ = await correct_transcript(
        Broken(), original, proxy_path=proxy, duration_sec=40, workdir=tmp_path
    )
    assert fixed is original and not fixed.corrected

    llm = FakeGeminiClient()
    llm.transcript_fix = lambda draft: "butunlay boshqa matn umuman aloqasi yoq gaplar"
    fixed, _ = await correct_transcript(llm, original, proxy_path=proxy, duration_sec=40, workdir=tmp_path)
    assert fixed is original  # rejected: too different

    done = original.model_copy(update={"corrected": True})
    again = FakeGeminiClient()
    fixed, _ = await correct_transcript(again, done, proxy_path=proxy, duration_sec=40, workdir=tmp_path)
    assert fixed is done and again.fix_calls == []  # never corrected twice


async def test_long_videos_are_corrected_window_by_window(
    proxy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.ai.transcript_fix.WINDOW_SEC", 20)
    words = [(f"soz{i}", i * 1.0, i * 1.0 + 0.5) for i in range(38)]
    llm = FakeGeminiClient()
    fixed, _ = await correct_transcript(
        llm, make_transcript(words), proxy_path=proxy, duration_sec=40, workdir=tmp_path
    )
    assert len(llm.fix_calls) == 2 and llm.fix_calls[1].split()[0] == "soz20"  # each word in one window
    assert [w.text for w in fixed.all_words()] == [w for w, _, _ in words]
