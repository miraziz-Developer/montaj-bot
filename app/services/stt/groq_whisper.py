import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from app.services.stt.base import STTError, Transcript, TranscriptSegment, Word

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


def _word(raw: dict[str, Any]) -> Word:
    return Word(
        text=str(raw.get("word", raw.get("text", ""))).strip(),
        start=float(raw["start"]),
        end=float(raw["end"]),
    )


def _approximate_words(segment: TranscriptSegment) -> list[Word]:
    """No word timestamps from the API: spread the segment evenly over its words."""
    tokens = segment.text.split()
    if not tokens:
        return []
    step = (segment.end - segment.start) / len(tokens)
    return [
        Word(text=tok, start=segment.start + i * step, end=segment.start + (i + 1) * step)
        for i, tok in enumerate(tokens)
    ]


class GroqSTTProvider:
    _warned_no_words = False  # warn once per process, not per chunk

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        client: httpx.AsyncClient | None = None,
        url: str = GROQ_URL,
        timeout: float = 300,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client = client
        self._url = url
        self._timeout = timeout
        self._sleep = sleep

    async def transcribe(self, audio_path: Path, *, language_hint: str | None) -> Transcript:
        if not self._api_key:
            raise STTError("GROQ_API_KEY is not configured")
        audio = await asyncio.to_thread(audio_path.read_bytes)
        # VERIFY (console.groq.com/docs/speech-to-text): timestamp_granularities[] accepts "word"/"segment"
        data: dict[str, Any] = {
            "model": self._model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": ["word", "segment"],
            "temperature": "0",
        }
        if language_hint:
            data["language"] = language_hint
        payload = await self._post(audio_path.name, audio, data)
        return self.parse_response(payload)

    async def _post(self, filename: str, audio: bytes, data: dict[str, Any]) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    response = await client.post(
                        self._url,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        data=data,
                        files={"file": (filename, audio, "audio/ogg")},
                    )
                except httpx.TransportError as exc:
                    if attempt == MAX_ATTEMPTS:
                        raise STTError(f"Groq request failed: {type(exc).__name__}") from exc
                else:
                    if response.status_code == 200:
                        return response.json()
                    if response.status_code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS:
                        raise STTError(f"Groq returned HTTP {response.status_code}: {response.text[:300]}")
                logger.warning("Groq STT attempt %s/%s failed, retrying", attempt, MAX_ATTEMPTS)
                await self._sleep(2 ** (attempt - 1))
        finally:
            if self._client is None:
                await client.aclose()
        raise STTError("Groq request failed")  # pragma: no cover (loop always returns or raises)

    @classmethod
    def parse_response(cls, payload: dict[str, Any]) -> Transcript:
        """Handles words at top level (OpenAI style) or nested per segment."""
        language = str(payload.get("language") or "")
        raw_segments = payload.get("segments") or []
        segments = [
            TranscriptSegment(
                start=float(raw["start"]),
                end=float(raw["end"]),
                text=str(raw.get("text", "")).strip(),
                words=[_word(w) for w in raw.get("words") or []],
            )
            for raw in raw_segments
        ]
        if not segments and str(payload.get("text", "")).strip():
            duration = float(payload.get("duration") or 0)
            segments = [TranscriptSegment(start=0.0, end=duration, text=str(payload["text"]).strip())]

        top_words = [_word(w) for w in payload.get("words") or []]
        if top_words and segments:
            for word in top_words:
                mid = (word.start + word.end) / 2
                target = next((s for s in segments if s.start <= mid < s.end), segments[-1])
                target.words.append(word)

        if segments and not any(s.words for s in segments):
            if not cls._warned_no_words:
                logger.warning("Groq returned no word timestamps: approximating from segments")
                cls._warned_no_words = True
            for segment in segments:
                segment.words = _approximate_words(segment)
        return Transcript(language=language, segments=segments)
