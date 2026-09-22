from pathlib import Path

import httpx
import pytest

from app.services.stt.base import STTError, Transcript, TranscriptSegment, Word
from app.services.stt.fake import FakeSTTProvider
from app.services.stt.groq_whisper import GroqSTTProvider


def _w(text: str, start: float, end: float) -> Word:
    return Word(text=text, start=start, end=end)


def _transcript() -> Transcript:
    return Transcript(
        language="uz",
        segments=[
            TranscriptSegment(start=0, end=2, text="a b", words=[_w("a", 0, 1), _w("b", 1, 2)]),
            TranscriptSegment(start=2, end=3, text="c", words=[_w("c", 2, 3)]),
        ],
    )


# ---------- Transcript ----------


def test_all_words_flattens_in_order() -> None:
    assert [w.text for w in _transcript().all_words()] == ["a", "b", "c"]


def test_words_in_boundaries() -> None:
    t = _transcript()
    assert [w.text for w in t.words_in(1, 2)] == ["b"]  # touching neighbours are excluded
    assert [w.text for w in t.words_in(0.5, 1.5)] == ["a", "b"]  # partial overlap counts
    assert [w.text for w in t.words_in(0, 3)] == ["a", "b", "c"]
    assert t.words_in(3, 4) == [] and t.words_in(-1, 0) == []
    assert [w.text for w in t.words_in(2.999, 10)] == ["c"]


def test_offset_shifts_segments_and_words_without_mutating() -> None:
    t = _transcript()
    shifted = t.offset(10)
    assert (shifted.segments[0].start, shifted.segments[1].words[0].end) == (10, 13)
    assert t.segments[0].start == 0


def test_merge_orders_by_time_and_picks_first_language() -> None:
    late = Transcript(language="", segments=[TranscriptSegment(start=10, end=11, text="z")])
    early = Transcript(language="uz", segments=[TranscriptSegment(start=1, end=2, text="y")])
    merged = Transcript.merge([late, early])
    assert [s.text for s in merged.segments] == ["y", "z"] and merged.language == "uz"


# ---------- FakeSTTProvider ----------


async def test_fake_provider_scripts_by_file_name_and_records_calls() -> None:
    fake = FakeSTTProvider(by_name={"audio_000.ogg": _transcript()})
    got = await fake.transcribe(Path("/x/audio_000.ogg"), language_hint="uz")
    other = await fake.transcribe(Path("/x/audio_009.ogg"), language_hint=None)
    assert len(got.segments) == 2 and other.segments == []
    assert fake.calls == [("audio_000.ogg", "uz"), ("audio_009.ogg", None)]
    got.segments.clear()  # results are copies
    assert len((await fake.transcribe(Path("audio_000.ogg"), language_hint=None)).segments) == 2


# ---------- Groq response parsing ----------


def test_parse_top_level_words_are_assigned_to_segments() -> None:
    payload = {
        "language": "uzbek",
        "segments": [
            {"start": 0, "end": 2, "text": " salom dunyo "},
            {"start": 2, "end": 4, "text": "yaxshi"},
        ],
        "words": [
            {"word": "salom", "start": 0.1, "end": 0.9},
            {"word": "dunyo", "start": 1.0, "end": 1.8},
            {"word": "yaxshi", "start": 2.2, "end": 3.0},
        ],
    }
    t = GroqSTTProvider.parse_response(payload)
    assert t.language == "uzbek"
    assert [[w.text for w in s.words] for s in t.segments] == [["salom", "dunyo"], ["yaxshi"]]
    assert t.segments[0].text == "salom dunyo"


def test_parse_nested_words() -> None:
    payload = {
        "segments": [{"start": 0, "end": 1, "text": "hi", "words": [{"word": "hi", "start": 0, "end": 1}]}]
    }
    assert [w.text for w in GroqSTTProvider.parse_response(payload).all_words()] == ["hi"]


def test_parse_without_word_timestamps_approximates_evenly() -> None:
    payload = {"segments": [{"start": 10, "end": 14, "text": "bir ikki uch to'rt"}]}
    words = GroqSTTProvider.parse_response(payload).all_words()
    assert [(w.text, w.start, w.end) for w in words] == [
        ("bir", 10, 11), ("ikki", 11, 12), ("uch", 12, 13), ("to'rt", 13, 14),
    ]  # fmt: skip


def test_parse_text_only_and_empty() -> None:
    only_text = GroqSTTProvider.parse_response({"text": "hello", "duration": 3})
    assert (only_text.segments[0].start, only_text.segments[0].end) == (0, 3)
    assert GroqSTTProvider.parse_response({"language": "uz", "segments": [], "text": ""}).segments == []


# ---------- Groq HTTP behaviour ----------

OK = {
    "language": "uz",
    "segments": [{"start": 0, "end": 1, "text": "hi", "words": [{"word": "hi", "start": 0, "end": 1}]}],
}


def _provider(handler, sleeps: list[float] | None = None) -> GroqSTTProvider:
    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GroqSTTProvider("SECRET-KEY", "whisper-large-v3-turbo", client=client, sleep=sleep)


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    path = tmp_path / "audio_000.ogg"
    path.write_bytes(b"OggS-fake-audio")
    return path


async def test_groq_request_shape(audio: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OK)

    transcript = await _provider(handler).transcribe(audio, language_hint="uz")
    [request] = seen
    body = request.content
    assert request.headers["authorization"] == "Bearer SECRET-KEY"
    assert str(request.url) == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert b'name="model"' in body and b"whisper-large-v3-turbo" in body
    assert b"verbose_json" in body and body.count(b'name="timestamp_granularities[]"') == 2
    assert b'name="language"' in body and b'filename="audio_000.ogg"' in body
    assert transcript.all_words()[0].text == "hi"


async def test_groq_omits_language_when_no_hint(audio: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert b'name="language"' not in request.content
        return httpx.Response(200, json=OK)

    await _provider(handler).transcribe(audio, language_hint=None)


async def test_groq_retries_5xx_then_succeeds(audio: Path) -> None:
    calls, sleeps = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json=OK)

    await _provider(handler, sleeps).transcribe(audio, language_hint=None)
    assert len(calls) == 3 and sleeps == [1, 2]


async def test_groq_retries_transport_errors(audio: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json=OK)

    await _provider(handler).transcribe(audio, language_hint=None)
    assert len(calls) == 2


async def test_groq_gives_up_after_three_attempts(audio: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, text="slow down")

    with pytest.raises(STTError, match="429"):
        await _provider(handler).transcribe(audio, language_hint=None)
    assert len(calls) == 3


async def test_groq_client_errors_are_not_retried_and_never_leak_the_key(audio: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, text="invalid api key")

    with pytest.raises(STTError) as exc:
        await _provider(handler).transcribe(audio, language_hint=None)
    assert len(calls) == 1 and "SECRET-KEY" not in str(exc.value)


async def test_groq_without_an_api_key_fails_clearly_before_any_request(audio: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent without a key")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(STTError, match="GROQ_API_KEY"):
        await GroqSTTProvider("", "m", client=client).transcribe(audio, language_hint=None)
