"""Style presets: docs/EDIT_PLAN_SCHEMA.md section 3."""

from dataclasses import dataclass
from typing import Any, Literal

from app.core.errors import NotFound


@dataclass(frozen=True, slots=True)
class PresetRules:
    key: str
    label: str  # Uzbek, shown to users (same names as the Mini App style cards)
    max_shot_sec: float
    zoom_levels: tuple[float, ...]  # alternate per clip
    crossfade_sec: float  # 0 = hard cuts; applied between clips by code, like zoom_levels
    remove_gap_sec: float  # silent gaps longer than this are cut out
    pad_sec: float  # breathing room kept on both sides of a cut
    captions_style: Literal["word_highlight", "pop", "karaoke", "classic"]
    captions_max_words: int
    captions_highlight_color: str
    music_volume: float
    target_min_sec: float | None  # None = no limit
    target_max_sec: float | None
    keep_chronology: bool


PRESETS: dict[str, PresetRules] = {
    "dynamic_reels": PresetRules(
        key="dynamic_reels",
        label="Dinamik Reels",
        max_shot_sec=5,
        zoom_levels=(1.0, 1.15),
        crossfade_sec=0.12,
        remove_gap_sec=0.30,
        pad_sec=0.08,
        captions_style="pop",
        captions_max_words=3,
        captions_highlight_color="#FFD400",
        music_volume=0.10,
        target_min_sec=15,
        target_max_sec=60,
        keep_chronology=False,
    ),
    "clean_talk": PresetRules(
        key="clean_talk",
        label="Sokin gap",
        max_shot_sec=12,
        zoom_levels=(1.0, 1.05),
        crossfade_sec=0.20,
        remove_gap_sec=0.50,
        pad_sec=0.10,
        captions_style="classic",
        captions_max_words=5,
        captions_highlight_color="#FFD400",
        music_volume=0.05,
        target_min_sec=None,
        target_max_sec=None,
        keep_chronology=False,
    ),
    "ad_commercial": PresetRules(
        key="ad_commercial",
        label="Reklama",
        max_shot_sec=4,
        zoom_levels=(1.0, 1.10),
        crossfade_sec=0.08,
        remove_gap_sec=0.30,
        pad_sec=0.08,
        captions_style="word_highlight",
        captions_max_words=3,
        captions_highlight_color="#FFD400",
        music_volume=0.14,
        target_min_sec=15,
        target_max_sec=45,
        keep_chronology=False,
    ),
    "vlog_story": PresetRules(
        key="vlog_story",
        label="Vlog/hikoya",
        max_shot_sec=8,
        zoom_levels=(1.0, 1.04),
        crossfade_sec=0.25,
        remove_gap_sec=0.60,
        pad_sec=0.12,
        captions_style="classic",
        captions_max_words=4,
        captions_highlight_color="#FFD400",
        music_volume=0.08,
        target_min_sec=None,
        target_max_sec=None,
        keep_chronology=True,
    ),
}


def get_preset(key: str) -> PresetRules:
    try:
        return PRESETS[key]
    except KeyError:
        raise NotFound() from None


def preset_rules_dict(preset: PresetRules) -> dict[str, Any]:
    """The `preset_rules` object embedded in the planner prompt (RUNTIME_PROMPTS.md B.2)."""
    return {
        "max_shot_sec": preset.max_shot_sec,
        "remove_gap_sec": preset.remove_gap_sec,
        "pad_sec": preset.pad_sec,
        "target_min_sec": preset.target_min_sec,
        "target_max_sec": preset.target_max_sec,
        "music_volume": preset.music_volume,
        "keep_chronology": preset.keep_chronology,
        "captions": {
            "style": preset.captions_style,
            "max_words_per_line": preset.captions_max_words,
            "highlight_color": preset.captions_highlight_color,
        },
    }
