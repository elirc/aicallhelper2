"""Window-bounds sanitization geometry cases."""

from __future__ import annotations

from app_core.store.bounds import SanitizedBounds, WorkArea, sanitize_bounds

PRIMARY = WorkArea(left=0, top=0, right=1920, bottom=1040)
LEFT_MONITOR = WorkArea(left=-1920, top=0, right=0, bottom=1080)


def sane(raw: object, areas: list[WorkArea] | None = None) -> SanitizedBounds | None:
    return sanitize_bounds(
        raw, min_width=380, min_height=520, work_areas=areas if areas is not None else [PRIMARY]
    )


class TestCorruption:
    def test_non_dict_dropped(self) -> None:
        for raw in (None, "x", 5, [1, 2, 3, 4], True):
            assert sane(raw) is None

    def test_missing_field_drops_the_unit(self) -> None:
        assert sane({"x": 1, "y": 2, "width": 500}) is None

    def test_non_numeric_field_drops_the_unit(self) -> None:
        assert sane({"x": "a", "y": 0, "width": 500, "height": 600}) is None
        assert sane({"x": 0, "y": None, "width": 500, "height": 600}) is None

    def test_bool_is_not_a_coordinate(self) -> None:
        assert sane({"x": True, "y": 0, "width": 500, "height": 600}) is None

    def test_nan_and_inf_dropped(self) -> None:
        assert sane({"x": float("nan"), "y": 0, "width": 500, "height": 600}) is None
        assert sane({"x": float("inf"), "y": 0, "width": 500, "height": 600}) is None


class TestSizeClamping:
    def test_size_clamps_up_to_minimum(self) -> None:
        result = sane({"x": 100, "y": 100, "width": 100, "height": 50})
        assert result == SanitizedBounds(width=380, height=520, x=100, y=100)

    def test_floats_rounded_to_integers(self) -> None:
        result = sane({"x": 100.6, "y": 99.4, "width": 500.5, "height": 600.2})
        assert result is not None
        assert (result.x, result.y) == (101, 99)
        assert (result.width, result.height) in ((500, 600), (501, 600))


class TestVisibility:
    def test_fully_on_screen_keeps_position(self) -> None:
        result = sane({"x": 200, "y": 200, "width": 460, "height": 700})
        assert result is not None and result.x == 200 and result.y == 200

    def test_offscreen_drops_position_keeps_size(self) -> None:
        result = sane({"x": 5000, "y": 5000, "width": 460, "height": 700})
        assert result == SanitizedBounds(width=460, height=700, x=None, y=None)

    def test_39px_visible_is_not_enough_40_is(self) -> None:
        # Window hangs off the left edge; overlap = x + width.
        result = sane({"x": -460 + 39, "y": 100, "width": 460, "height": 700})
        assert result is not None and result.x is None
        result = sane({"x": -460 + 40, "y": 100, "width": 460, "height": 700})
        assert result is not None and result.x == -420

    def test_both_axes_must_overlap(self) -> None:
        # Plenty of horizontal overlap but above the work area vertically.
        result = sane({"x": 100, "y": -700 + 39, "width": 460, "height": 700})
        assert result is not None and result.x is None

    def test_negative_coordinates_valid_on_left_monitor(self) -> None:
        result = sane(
            {"x": -1500, "y": 100, "width": 460, "height": 700},
            areas=[PRIMARY, LEFT_MONITOR],
        )
        assert result is not None and result.x == -1500

    def test_unplugged_monitor_recenters(self) -> None:
        # Same bounds, but the left monitor is gone: position dropped.
        result = sane({"x": -1500, "y": 100, "width": 460, "height": 700}, areas=[PRIMARY])
        assert result is not None and result.x is None

    def test_visibility_judged_at_clamped_size(self) -> None:
        # Tiny stored size would fail the 40px test at face value, but the
        # CLAMPED window is large enough to be visible.
        result = sane({"x": -100, "y": 100, "width": 10, "height": 10})
        assert result is not None
        assert result.width == 380 and result.x == -100

    def test_no_displays_drops_position(self) -> None:
        result = sane({"x": 100, "y": 100, "width": 460, "height": 700}, areas=[])
        assert result == SanitizedBounds(width=460, height=700, x=None, y=None)
