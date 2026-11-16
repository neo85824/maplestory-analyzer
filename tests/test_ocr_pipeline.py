"""End-to-end golden-value regression: real capture crop -> real RapidOCR ->
real parser, against the one known-good screenshot in samples/. This is the
only test that exercises actual OCR inference (slow-ish, one-time model load)
-- it exists to catch regressions in regions.py's boxes, the OCR wrapper, or
the parser regexes that unit tests on synthetic strings wouldn't catch, since
those don't touch real recognition output.

Ground truth read off samples/maple_story_new_ui_1366.png (new UI, client
1366x768): LV.51, HP[373/922], MP[723/3290], EXP252463[33.72%].

It is also the only proof that regions.py's measured pixel offsets land on the
text. One real sample is checked in, at the resolution the tool is used at. A
resized copy of it would prove nothing about another resolution -- the game
re-renders its bar at native size -- so more coverage needs a real capture.
"""
import pytest

from maple_analyzer.capture import StaticImageCapture
from maple_analyzer.ocr import StatPanelOcr
from maple_analyzer.parser import parse_fields

from conftest import SAMPLE_IMAGE


@pytest.fixture(scope="module")
def ocr_engine():
    return StatPanelOcr()  # loads the ONNX model once, reused across tests in this file


@pytest.fixture(scope="module")
def snapshot(ocr_engine):
    capture = StaticImageCapture(SAMPLE_IMAGE)
    fields = capture.grab_fields()
    field_text = {name: ocr_engine.read_field(img, name) for name, img in fields.items()}
    return parse_fields(field_text)


def test_hp(snapshot):
    assert (snapshot.hp_cur, snapshot.hp_max) == (373, 922)


def test_mp(snapshot):
    assert (snapshot.mp_cur, snapshot.mp_max) == (723, 3290)


def test_exp(snapshot):
    assert snapshot.exp_cur == 252463
    assert snapshot.exp_pct == 33.72


def test_level(snapshot):
    # The two-tile orange LV badge only reads correctly with the LV-specific
    # binarisation in ocr.read_field (plain greyscale gives '50' for '51').
    assert snapshot.level == 51


def test_field_crops_are_nonempty(ocr_engine):
    capture = StaticImageCapture(SAMPLE_IMAGE)
    fields = capture.grab_fields()
    assert set(fields.keys()) == {"LV", "HP", "MP", "EXP"}
    for name, img in fields.items():
        assert img.width > 0 and img.height > 0, f"{name} crop is empty"
