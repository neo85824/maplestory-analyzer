"""Occlusion detection: is another window covering the stat panel?

`mss` grabs a screen *region*, not the game's pixels, so anything resting on
that strip is what reaches OCR. Live capture (2026-08-17) caught the app
reading its own log back off a terminal covering the panel, which parsed as
MP 1/2 and booked the whole bar as loss.

The Win32 lookup is injected, so the logic here is testable off Windows like
the rest of the suite.
"""
from __future__ import annotations

import pytest

from maple_analyzer.capture import field_sample_points, panel_is_obscured
from maple_analyzer.regions import field_boxes

GAME = 1000
OTHER = 2000

CLIENT = (1366, 768)
POINTS = field_sample_points(CLIENT)


def _always(hwnd):
    return lambda _x, _y: hwnd


def test_clear_panel_is_not_flagged():
    assert panel_is_obscured(POINTS, GAME, _always(GAME)) is False


def test_fully_covered_panel_is_flagged():
    assert panel_is_obscured(POINTS, GAME, _always(OTHER)) is True


@pytest.mark.parametrize("index", range(len(POINTS)))
def test_partially_covered_panel_is_flagged(index):
    """Partial coverage is the *dangerous* case, not a lesser one: a window
    clipping only some digits leaves the field's label readable, so the value
    still parses -- just wrong. Any covered sample point must flag."""
    covered = POINTS[index]

    def window_at(x, y):
        return OTHER if (x, y) == covered else GAME

    assert panel_is_obscured(POINTS, GAME, window_at) is True


def test_game_child_windows_do_not_count_as_covering():
    """window_at resolves to the *root* window, so the game's own child
    windows/controls read as the game. A false positive here would stop
    tracking entirely, which is worse than the bug being fixed."""
    assert panel_is_obscured(POINTS, GAME, _always(GAME)) is False


def test_sample_points_lie_inside_every_field_box():
    """Guards an off-by-one that would probe just outside the fields and so
    never detect anything -- a silently useless check."""
    boxes = field_boxes(CLIENT)
    hit = {name: 0 for name in boxes}
    for name, box in boxes.items():
        for x, y in POINTS:
            if box.left <= x < box.right and box.top <= y < box.bottom:
                hit[name] += 1
    assert all(count > 0 for count in hit.values()), hit
    # every point must belong to some field box
    assert sum(hit.values()) >= len(POINTS)


def test_sample_points_track_the_client_width():
    """HP/MP/EXP are right-anchored, so a wider client pushes their sample
    points further right."""
    small = field_sample_points((1280, 720))
    large = field_sample_points((1920, 720))
    assert len(small) == len(large)
    assert max(x for x, _ in large) > max(x for x, _ in small)


def test_obscured_message_is_localised():
    """The three capture states are routine, expected conditions users hit
    constantly, so they get real translations rather than leaking raw English
    into a zh UI -- same treatment as minimized/not-found."""
    from maple_analyzer.capture import PANEL_OBSCURED
    from maple_analyzer.overlay import OverlayApp
    from maple_analyzer.settings import Settings

    class Stub:
        _settings = Settings()  # zh by default
        _t = OverlayApp._t
        _localize_error = OverlayApp._localize_error

    stub = Stub()
    localized = stub._localize_error(PANEL_OBSCURED)
    assert localized != PANEL_OBSCURED
    assert "被" in localized  # zh string, not raw English

    stub._settings = Settings(language="en")
    assert stub._localize_error(PANEL_OBSCURED) == "Stat panel is covered"
