import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from app.services.stt.base import STTError, Transcript, TranscriptSegment, Word

logger = logging.getLogger(__name__)

API_VERSION = "2024-11-15"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


def _word(raw: dict[str, Any]) -> Word:
    start = raw["offsetMilliseconds"] / 1000
    return Word(text=str(raw["text"]), start=start, end=start + raw["durationMilliseconds"] / 1000)


def _segment(raw: dict[str, Any]) -> TranscriptSegment:
    start = raw["offsetMilliseconds"] / 1000
    return TranscriptSegment(
        start=start,
        end=start + raw["durationMilliseconds"] / 1000,
        text=str(raw["text"]),
        words=[_word(w) for w in raw.get("words", [])],
    )


class AzureSpeechSTTProvider:
    """Azure AI Speech "Fast Transcription" API (single-shot, like Whisper): POST audio, get back segments +
    word-level timestamps in one response. Endpoint is the Cognitive Services / AI Foundry project resource
    (e.g. https://<project>.cognitiveservices.azure.com), auth via Ocp-Apim-Subscription-Key.

    `locales` are auto-detection CANDIDATES, not a fixed choice: Azure picks the best-matching one per audio
    and reports it in each phrase's `locale`. Put every language your users actually speak here (docs: Uzbek,
    Russian or mixed) - a language spoken but not listed is misrecognized as the nearest candidate instead.

    # VERIFY: verified structurally (locales accepted, word timestamps returned) against a real Azure resource
    with synthetic English speech; not verified for real Uzbek/Russian audio quality.
    """

    def __init__(
        self,
        api_key: str,
        endpoint: str,
        locales: list[str],
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 300,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint.rstrip("/")
        self._locales = locales
        self._client = client
        self._timeout = timeout
        self._sleep = sleep

    async def transcribe(self, audio_path: Path, *, language_hint: str | None) -> Transcript:
        if not self._api_key:
            raise STTError("AZURE_SPEECH_API_KEY is not configured")
        if not self._endpoint:
            raise STTError("AZURE_SPEECH_ENDPOINT is not configured")
        audio = await asyncio.to_thread(audio_path.read_bytes)
        locales = [language_hint] if language_hint else self._locales
        payload = await self._post(audio_path.name, audio, locales)
        return self.parse_response(payload)

    async def _post(self, filename: str, audio: bytes, locales: list[str]) -> dict[str, Any]:
        url = f"{self._endpoint}/speechtotext/transcriptions:transcribe?api-version={API_VERSION}"
        definition = json.dumps({"locales": locales})
        mime = "audio/ogg" if filename.endswith(".ogg") else "audio/wav"
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    response = await client.post(
                        url,
                        headers={"Ocp-Apim-Subscription-Key": self._api_key},
                        files={
                            "definition": (None, definition, "application/json"),
                            "audio": (filename, audio, mime),
                        },
                    )
                except httpx.TransportError as exc:
                    if attempt == MAX_ATTEMPTS:
                        raise STTError(f"Azure Speech request failed: {type(exc).__name__}") from exc
                else:
                    if response.status_code == 200:
                        return response.json()
                    if response.status_code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS:
                        raise STTError(
                            f"Azure Speech returned HTTP {response.status_code}: {response.text[:300]}"
                        )
                logger.warning("Azure Speech STT attempt %s/%s failed, retrying", attempt, MAX_ATTEMPTS)
                await self._sleep(2 ** (attempt - 1))
        finally:
            if self._client is None:
                await client.aclose()
        raise STTError("Azure Speech request failed")  # pragma: no cover (loop always returns or raises)

    @classmethod
    def parse_response(cls, payload: dict[str, Any]) -> Transcript:
        phrases = payload.get("phrases", [])
        language = str(phrases[0]["locale"]) if phrases else ""
        return Transcript(language=language, segments=[_segment(p) for p in phrases])
