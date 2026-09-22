from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class STTError(Exception):
    """Speech-to-text provider failed (after retries)."""


class Word(BaseModel):
    text: str
    start: float
    end: float


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str
    words: list[Word] = []


class Transcript(BaseModel):
    language: str
    segments: list[TranscriptSegment]

    def all_words(self) -> list[Word]:
        return [word for segment in self.segments for word in segment.words]

    def words_in(self, start: float, end: float) -> list[Word]:
        """Words fully or partially inside [start, end): a word ending exactly at `start`, or starting
        exactly at `end`, is outside."""
        return [w for w in self.all_words() if w.end > start and w.start < end]

    def offset(self, seconds: float) -> "Transcript":
        """A copy with all times shifted by `seconds` (places chunk transcripts on the video timeline)."""
        return Transcript(
            language=self.language,
            segments=[
                TranscriptSegment(
                    start=s.start + seconds,
                    end=s.end + seconds,
                    text=s.text,
                    words=[Word(text=w.text, start=w.start + seconds, end=w.end + seconds) for w in s.words],
                )
                for s in self.segments
            ],
        )

    @classmethod
    def merge(cls, parts: Sequence["Transcript"]) -> "Transcript":
        segments = sorted((s for part in parts for s in part.segments), key=lambda s: s.start)
        language = next((p.language for p in parts if p.language), "")
        return cls(language=language, segments=segments)


class STTProvider(Protocol):
    async def transcribe(self, audio_path: Path, *, language_hint: str | None) -> Transcript: ...
