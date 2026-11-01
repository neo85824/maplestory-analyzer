"""Edge-anchored crop geometry -- pure, no images/OCR."""
import pytest

from maple_analyzer.regions import field_boxes, panel_box

CLIENT_SIZES = [(1024, 768), (1280, 720), (1366, 768), (1920, 1080), (2560, 1440)]


@pytest.mark.parametrize("client", CLIENT_SIZES)
def test_field_boxes_lie_inside_the_panel_box(client):
    panel = panel_box(client)
    for name, b in field_boxes(client).items():
        assert panel.left <= b.left < b.right <= panel.right, name
        assert panel.top <= b.top < b.bottom <= panel.bottom, name


@pytest.mark.parametrize("client", CLIENT_SIZES)
def test_boxes_stay_within_the_client_and_are_usably_sized(client):
    cw, ch = client
    for name, b in field_boxes(client).items():
        assert 0 <= b.left < b.right <= cw, name
        assert 0 <= b.top < b.bottom <= ch, name
        assert b.right - b.left >= 40, f"{name} too narrow at {client}"
        assert b.bottom - b.top >= 10, f"{name} too short at {client}"


@pytest.mark.parametrize("client", CLIENT_SIZES)
def test_all_boxes_are_bottom_anchored(client):
    """Same fixed distance from the client's bottom edge at every resolution."""
    ch = client[1]
    ups = {name: (ch - b.top, ch - b.bottom) for name, b in field_boxes(client).items()}
    ref = {name: (768 - b.top, 768 - b.bottom) for name, b in field_boxes((1366, 768)).items()}
    assert ups == ref


def test_lv_is_left_anchored_and_hp_mp_exp_are_right_anchored():
    narrow = field_boxes((1280, 720))
    wide = field_boxes((1920, 720))
    assert narrow["LV"].left == wide["LV"].left  # LV pinned to the left edge
    for name in ("HP", "MP", "EXP"):
        # right-pinned: box shifts right by exactly the width delta
        assert wide[name].left - narrow[name].left == 1920 - 1280, name
        assert wide[name].right - narrow[name].right == 1920 - 1280, name
