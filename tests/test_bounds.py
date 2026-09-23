"""Window-bounds sanitization geometry cases."""

from __future__ import annotations

from app_core.store.bounds import (
    Placement,
    SanitizedBounds,
    WorkArea,
    plan_restore,
    primary_area,
    sanitize_bounds,
    top_center,
)

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


class TestDocking:
    LAPTOP = WorkArea(left=0, top=0, right=1280, bottom=672)

    def test_top_center_centres_horizontally_against_the_top_margin(self) -> None:
        assert top_center(self.LAPTOP, 720, 260, 8) == (280, 8, 720, 260)

    def test_oversize_window_is_shrunk_to_fit_the_work_area(self) -> None:
        # The default 460x700 is taller than a 672 px laptop work area.
        assert top_center(self.LAPTOP, 460, 700, 8) == (410, 8, 460, 656)
        assert top_center(self.LAPTOP, 5000, 100, 0) == (0, 0, 1280, 100)

    def test_negative_origin_display_is_handled(self) -> None:
        assert top_center(LEFT_MONITOR, 720, 260, 8) == (-1320, 8, 720, 260)

    def test_primary_is_the_area_containing_the_origin_regardless_of_order(self) -> None:
        assert primary_area([LEFT_MONITOR, PRIMARY]) == PRIMARY
        assert primary_area([PRIMARY, LEFT_MONITOR]) == PRIMARY

    def test_primary_falls_back_to_first_then_none(self) -> None:
        assert primary_area([LEFT_MONITOR]) == LEFT_MONITOR
        assert primary_area([]) is None


ABOVE_MONITOR = WorkArea(left=0, top=-1080, right=1920, bottom=0)


class TestTitleBarReachability:
    """R12: 40 px of overlap is not enough — a window whose body overlaps a
    display while its title bar sits above it cannot be dragged back."""

    def test_title_bar_above_the_work_area_drops_the_position(self) -> None:
        # 300 px of the body overlaps the display, the title bar does not.
        result = sane({"x": 100, "y": -400, "width": 460, "height": 700})
        assert result is not None and result.x is None

    def test_a_maximized_frame_offset_is_still_restored(self) -> None:
        result = sane({"x": -8, "y": -8, "width": 1936, "height": 1056})
        assert result is not None and (result.x, result.y) == (-8, -8)

    def test_an_oversized_window_with_a_reachable_title_bar_is_restored(self) -> None:
        result = sane({"x": 100, "y": 10, "width": 3000, "height": 3000})
        assert result is not None and (result.x, result.y) == (100, 10)

    def test_negative_vertical_coordinates_on_a_display_above(self) -> None:
        result = sane(
            {"x": 100, "y": -1000, "width": 460, "height": 700},
            areas=[PRIMARY, ABOVE_MONITOR],
        )
        assert result is not None and result.y == -1000


def plan(raw: object, areas: list[WorkArea], dock: WorkArea | None = None) -> Placement:
    return plan_restore(
        raw,
        min_width=380,
        min_height=160,
        default_size=(720, 260),
        work_areas=areas,
        dock_area=dock,
        margin=8,
    )


SAVED_ON_LEFT = {"x": -1500, "y": 100, "width": 800, "height": 200}


class TestPlanRestore:
    def test_a_position_on_any_connected_display_is_restored(self) -> None:
        # The window currently sits on PRIMARY; the saved spot is on LEFT.
        assert plan(SAVED_ON_LEFT, [PRIMARY, LEFT_MONITOR], dock=PRIMARY) == Placement(
            -1500, 100, 800, 200
        )

    def test_an_unplugged_display_docks_on_the_preferred_display_keeping_size(self) -> None:
        assert plan(SAVED_ON_LEFT, [PRIMARY], dock=PRIMARY) == Placement(560, 8, 800, 200)

    def test_without_a_preferred_display_the_primary_is_used(self) -> None:
        assert plan(None, [LEFT_MONITOR, PRIMARY]) == Placement(600, 8, 720, 260)

    def test_no_display_information_still_yields_the_mode_size(self) -> None:
        assert plan(SAVED_ON_LEFT, []) == Placement(None, None, 800, 200)
        assert plan(None, []) == Placement(None, None, 720, 260)

    def test_the_preferred_display_is_honoured_even_if_not_listed(self) -> None:
        # Enumerating displays failed but the window's own display resolved.
        assert plan(None, [], dock=LEFT_MONITOR) == Placement(-1320, 8, 720, 260)
