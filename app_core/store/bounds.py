"""Pure window-geometry helpers: restore-on-launch sanitizing and docking.

Sanitize rules (each a real failure mode from v2):
- corrupt bounds are dropped AS A UNIT while the rest of the settings file
  survives;
- size clamps UP to the window minimum and rounds to integers;
- the position is kept only if the TITLE BAR is reachable on some display's
  work area: at least 40 px of the window (at its CLAMPED size) overlaps the
  area horizontally, and its top edge lies inside the area (a few px of
  slack above, for the -8 px frame of a maximized window) with 40 px below
  it. A window whose body overlaps a display while its title bar sits above
  it cannot be dragged back. Otherwise the position is dropped and the
  caller places the window (the unplugged-monitor scenario);
- negative coordinates are valid (displays left of / above the primary).

Docking: `top_center` places a window against the top edge of a work area,
horizontally centred — the camera line on a laptop or a monitor with a
webcam on top. Units in == units out; the caller decides logical/physical.

`plan_restore` is the one decision both startup and layout switches use:
restore a saved position that is reachable on ANY connected display; else
dock on the preferred display; else on the primary; and with no display
information at all, still apply the mode's size (position left alone).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

MIN_VISIBLE_PX = 40
# A maximized window reports its frame ~8 px outside the work area.
TITLE_BAR_SLACK_PX = 16


@dataclass(frozen=True)
class WorkArea:
    """One display's work area as left/top/right/bottom virtual-screen edges."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


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
        if _title_bar_reachable(x, y, width, area):
            return SanitizedBounds(width=width, height=height, x=x, y=y)
    return SanitizedBounds(width=width, height=height, x=None, y=None)


def _title_bar_reachable(x: int, y: int, width: int, area: WorkArea) -> bool:
    overlap_x = min(x + width, area.right) - max(x, area.left)
    top_inside = area.top - TITLE_BAR_SLACK_PX <= y <= area.bottom - MIN_VISIBLE_PX
    return overlap_x >= MIN_VISIBLE_PX and top_inside


@dataclass(frozen=True)
class Placement:
    """Where to put a window. `x`/`y` None = keep the current position and
    only apply the size (no display information was available)."""

    x: int | None
    y: int | None
    width: int
    height: int


def plan_restore(
    stored_raw: object,
    *,
    min_width: int,
    min_height: int,
    default_size: tuple[int, int],
    work_areas: Sequence[WorkArea],
    dock_area: WorkArea | None = None,
    margin: int = 0,
) -> Placement:
    """Geometry for a layout mode from its saved bounds.

    `work_areas` must list EVERY connected display: a position saved on
    display A is still valid while the window currently sits on display B.
    `dock_area` (the window's current display) is used only for the fallback
    dock; without it the primary display is used.
    """
    stored = sanitize_bounds(
        stored_raw, min_width=min_width, min_height=min_height, work_areas=work_areas
    )
    if stored is not None and stored.x is not None and stored.y is not None:
        return Placement(stored.x, stored.y, stored.width, stored.height)
    width, height = (stored.width, stored.height) if stored is not None else default_size
    area = dock_area if dock_area is not None else primary_area(work_areas)
    if area is None:
        return Placement(None, None, width, height)
    x, y, w, h = top_center(area, width, height, margin)
    return Placement(x, y, w, h)


def top_center(
    area: WorkArea, width: int, height: int, margin: int = 0
) -> tuple[int, int, int, int]:
    """(x, y, width, height) for a window docked top-centre in `area`.

    A window larger than the area (minus the margin on every side) is shrunk
    to fit: a default window taller than a small laptop display must not
    land under the taskbar.
    """
    max_w = max(1, area.width - 2 * margin)
    max_h = max(1, area.height - 2 * margin)
    w = min(width, max_w)
    h = min(height, max_h)
    x = area.left + (area.width - w) // 2
    y = area.top + margin
    return x, y, w, h


def primary_area(areas: Sequence[WorkArea]) -> WorkArea | None:
    """The work area containing the virtual-screen origin (the primary
    display), else the first one listed, else None."""
    for area in areas:
        if area.left <= 0 < area.right and area.top <= 0 < area.bottom:
            return area
    return areas[0] if areas else None
