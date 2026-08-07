"""Pure window-geometry sanitizer for restore-on-launch.

Rules (each a real failure mode from v2):
- corrupt bounds are dropped AS A UNIT while the rest of the settings file
  survives;
- size clamps UP to the window minimum and rounds to integers;
- the position is kept only if at least 40 px of the window (judged at its
  CLAMPED size) lands on some display's work area on BOTH axes — otherwise
  the position is dropped and the OS centers the window (the
  unplugged-monitor scenario);
- negative coordinates are valid (displays left of the primary).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

MIN_VISIBLE_PX = 40


@dataclass(frozen=True)
class WorkArea:
    """One display's work area as left/top/right/bottom virtual-screen edges."""

    left: int
    top: int
    right: int
    bottom: int


@dataclass(frozen=True)
class SanitizedBounds:
    """Clamped size, plus a position only when it passed the visibility test."""

    width: int
    height: int
    x: int | None
    y: int | None


def _as_int(value: object) -> int | None:
    # bool is an int subclass; True as an x-coordinate is corruption, not 1.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return round(value)
    return None


def sanitize_bounds(
    raw: object,
    *,
    min_width: int,
    min_height: int,
    work_areas: Sequence[WorkArea],
) -> SanitizedBounds | None:
    """Sanitize persisted bounds. None = corrupt, drop the whole unit."""
    if not isinstance(raw, dict):
        return None
    x = _as_int(raw.get("x"))
    y = _as_int(raw.get("y"))
    width = _as_int(raw.get("width"))
    height = _as_int(raw.get("height"))
    if x is None or y is None or width is None or height is None:
        return None
    width = max(width, min_width)
    height = max(height, min_height)
    for area in work_areas:
        overlap_x = min(x + width, area.right) - max(x, area.left)
        overlap_y = min(y + height, area.bottom) - max(y, area.top)
        if overlap_x >= MIN_VISIBLE_PX and overlap_y >= MIN_VISIBLE_PX:
            return SanitizedBounds(width=width, height=height, x=x, y=y)
    return SanitizedBounds(width=width, height=height, x=None, y=None)
