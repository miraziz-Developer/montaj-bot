"""Beat-synced music: pick the music start offset so the video's cuts land on the beat.

The cuts themselves are never moved (they sit on word boundaries; moving them would chop speech). Instead the
track's beat grid is detected here (spectral-flux onsets + autocorrelation tempo + comb phase, numpy only)
and the music is started at the offset where the most cuts fall within BEAT_TOLERANCE_SEC of a beat."""

import asyncio
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SAMPLE_RATE = 22050
ANALYSIS_SEC = 90.0
FRAME = 1024
HOP = 256
MIN_BPM, MAX_BPM = 70.0, 180.0
PREFERRED_BPM = 120.0  # tempo prior: halves/doubles of the true tempo are resolved towards this
BEAT_TOLERANCE_SEC = 0.07
ONSET_LATENCY_SEC = 0.033  # measured on click tracks at 90-140 BPM: -26..-37 ms before correction
OFFSET_STEP_SEC = 0.01
FFMPEG_TIMEOUT_SEC = 60


@dataclass(frozen=True, slots=True)
class BeatGrid:
    period: float  # seconds per beat
    phase: float  # time of the first beat, 0 <= phase < period

    @property
    def bpm(self) -> float:
        return 60.0 / self.period


async def _decode(path: Path) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-nostdin", "-t", f"{ANALYSIS_SEC:g}", "-i", str(path),
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    try:
        out, err = await asyncio.wait_for(proc.communicate(), FFMPEG_TIMEOUT_SEC)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {err.decode(errors='replace')[-300:]}")
    return out


def beat_grid_from_samples(raw: bytes) -> BeatGrid | None:
    """Tempo and phase of mono float32 PCM at SAMPLE_RATE, or None if there is no steady pulse."""
    import numpy as np  # lazy, like cv2 in face_track: only the render path needs it

    audio = np.frombuffer(raw, dtype=np.float32)
    if audio.size < SAMPLE_RATE * 4:
        return None
    frames = 1 + (audio.size - FRAME) // HOP
    index = np.arange(FRAME)[None, :] + HOP * np.arange(frames)[:, None]
    spectrum = np.abs(np.fft.rfft(audio[index] * np.hanning(FRAME), axis=1))
    flux = np.maximum(np.diff(np.log1p(100 * spectrum), axis=0), 0).sum(axis=1)
    onset = flux - np.convolve(flux, np.ones(16) / 16, mode="same")  # remove the slow loudness trend
    onset = np.maximum(onset, 0)
    if onset.std() == 0:
        return None
    onset /= onset.std()
    fps = SAMPLE_RATE / HOP

    lags = np.arange(int(fps * 60 / MAX_BPM), int(fps * 60 / MIN_BPM) + 1)
    corr = np.array([np.dot(onset[:-lag], onset[lag:]) / (onset.size - lag) for lag in lags])
    bpms = 60 * fps / lags
    prior = np.exp(-0.5 * (np.log2(bpms / PREFERRED_BPM) / 0.9) ** 2)
    best = int(np.argmax(corr * prior))
    if corr[best] <= 1.15 * np.median(corr):  # no clear periodicity: ambient/rubato track
        return None
    period = float(lags[best]) / fps

    # onset[i] is the flux between STFT frames i and i+1; ONSET_LATENCY_SEC (calibrated on click tracks) maps
    # it to the moment the beat actually starts
    t0 = HOP / SAMPLE_RATE + ONSET_LATENCY_SEC
    grid_t = t0 + np.arange(onset.size) / fps

    def comb(per: float, phase: float) -> float:
        beats = np.arange(phase, grid_t[-1], per)
        return float(np.interp(beats, grid_t, onset).sum()) / max(len(beats), 1)

    # joint fine search: the autocorrelation lag is only frame-accurate, and a 0.5% tempo error drifts a
    # full beat over a minute, so tempo and phase are refined together on the comb
    best_score, best_period, best_phase = -1.0, period, 0.0
    for per in np.linspace(period * 0.985, period * 1.015, 61):
        for phase in np.linspace(0, per, 96, endpoint=False):
            score = comb(float(per), float(phase))
            if score > best_score:
                best_score, best_period, best_phase = score, float(per), float(phase)
    period = best_period
    return BeatGrid(period=period, phase=best_phase % period)


async def detect_beats(path: Path) -> BeatGrid | None:
    """Never raises: beat sync is a refinement, the music plays unsynced if analysis fails."""
    try:
        return await asyncio.to_thread(beat_grid_from_samples, await _decode(path))
    except Exception:
        logger.warning("beat detection failed for %s: music stays unsynced", path.name, exc_info=True)
        return None


def _distance_to_beat(t: float, grid: BeatGrid, offset: float) -> float:
    """Seconds from output time t to the nearest beat when the music starts `offset` s into the track."""
    x = (t + offset - grid.phase) / grid.period
    return abs(x - round(x)) * grid.period


def best_offset(grid: BeatGrid, cuts: Sequence[float], weights: Sequence[float] | None = None) -> float:
    """Music start offset in [0, period) that puts the most (weighted) cuts on a beat; ties go to the
    offset with the smallest total miss. Without cuts: start on the first beat (a clean downbeat entry)."""
    if not cuts:
        return grid.phase
    weights = weights or [1.0] * len(cuts)
    best, best_key = 0.0, (-1.0, 0.0)
    for step in range(math.ceil(grid.period / OFFSET_STEP_SEC)):
        offset = step * OFFSET_STEP_SEC
        misses = [_distance_to_beat(t, grid, offset) for t in cuts]
        hits = sum(w for m, w in zip(misses, weights, strict=True) if m <= BEAT_TOLERANCE_SEC)
        key = (hits, -sum(min(m, BEAT_TOLERANCE_SEC * 2) for m in misses))
        if key > best_key:
            best, best_key = offset, key
    return best


def on_beat_fraction(grid: BeatGrid, cuts: Sequence[float], offset: float) -> float:
    if not cuts:
        return 0.0
    return sum(_distance_to_beat(t, grid, offset) <= BEAT_TOLERANCE_SEC for t in cuts) / len(cuts)
