import json
import logging
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def resolve_music_track(track_id: str | None, assets_dir: Path | str | None = None) -> Path | None:
    """Path of a catalog track, or None (disabled / unknown / missing file). A safety net: the plan
    validator already dropped unknown tracks. Catalog files cannot point outside `assets/music`."""
    if not track_id:
        return None
    root = (Path(assets_dir) if assets_dir else Path(get_settings().assets_dir)) / "music"
    try:
        catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
        entry = next(item for item in catalog if item.get("id") == track_id)
        path = (root / str(entry["file"])).resolve()
    except (OSError, ValueError, StopIteration, KeyError, TypeError, AttributeError):
        logger.warning("music track %r not found in the catalog", track_id)
        return None
    if root.resolve() not in path.parents or not path.is_file():
        logger.warning("music track %r points outside assets/music or is missing", track_id)
        return None
    return path


def load_music_catalog(assets_dir: Path | str | None = None) -> list[dict[str, str]]:
    """`[{"id", "mood"}]` for the planner; empty without a valid catalog (music is then never used)."""
    root = (Path(assets_dir) if assets_dir else Path(get_settings().assets_dir)) / "music"
    try:
        catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
        return [{"id": str(t["id"]), "mood": str(t.get("mood", ""))} for t in catalog]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []
