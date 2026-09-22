from collections.abc import Mapping
from pathlib import Path

from app.services.stt.base import Transcript


class FakeSTTProvider:
    """Deterministic, network-free STT for tests.

    `by_name` maps an audio file name (e.g. "audio_000.ogg") to its transcript; other files get `default`.
    Every call is recorded in `calls` as (file name, language_hint).
    """

    def __init__(
        self, by_name: Mapping[str, Transcript] | None = None, default: Transcript | None = None
    ) -> None:
        self.by_name = dict(by_name or {})
        self.default = default or Transcript(language="uz", segments=[])
        self.calls: list[tuple[str, str | None]] = []

    async def transcribe(self, audio_path: Path, *, language_hint: str | None) -> Transcript:
        self.calls.append((audio_path.name, language_hint))
        return self.by_name.get(audio_path.name, self.default).model_copy(deep=True)
