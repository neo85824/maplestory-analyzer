"""Do the crops actually land on the HUD text in a real screenshot?

regions.py's boxes are fixed pixel insets from the client-area edges (the new-UI
HUD is fixed pixel size and edge-anchored -- see that module's docstring). The
game only offers a fixed list of client resolutions and the bar is
pixel-identical across all of them, so the edge-anchor invariants in
test_regions.py cover every resolution at once. This file pins the one thing
those can't: that the measured offsets are right, against the real screenshot.

Only one real sample is checked in (samples/maple_story_new_ui_1366.png, client
1366x768 -- the resolution the tool is used at). A resized copy of a screenshot
does not reproduce how the game re-renders its HUD, so it would prove nothing;
adding coverage at another resolution needs a real capture at that resolution.
"""
from __future__ import annotations

import pytest
from PIL import Image

from conftest import SAMPLE_IMAGE
from maple_analyzer.ocr import StatPanelOcr
from maple_analyzer.parser import parse_fields
from maple_analyzer.regions import field_boxes

TRUTH = {"hp": (373, 922), "mp": (723, 3290), "exp_cur": 252463, "exp_pct": 33.72}


@pytest.fixture(scope="module")
def snapshot():
    image = Image.open(SAMPLE_IMAGE).convert("RGB")
    ocr = StatPanelOcr()
    text = {
        name: ocr.read_field(image.crop(box.as_tuple()), name)
        for name, box in field_boxes(image.size).items()
    }
    return parse_fields(text), text


def test_hp_mp_read_correctly(snapshot):
    snap, text = snapshot
    assert (snap.hp_cur, snap.hp_max) == TRUTH["hp"], text
    assert (snap.mp_cur, snap.mp_max) == TRUTH["mp"], text


def test_exp_reads_correctly(snapshot):
    snap, text = snapshot
    assert snap.exp_cur == TRUTH["exp_cur"], text
    assert snap.exp_pct == TRUTH["exp_pct"], text
