import math


def compute_units(duration_sec: float, unit_seconds: int) -> int:
    """1 unit = up to `unit_seconds` of SOURCE video; always at least 1 unit."""
    return max(1, math.ceil(duration_sec / unit_seconds))
