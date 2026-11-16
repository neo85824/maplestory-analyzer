"""Integration smoke test: DemoExpFeed -> real OCR -> real parser, several
ticks in a row. Only the "camera" is fake here (see demo_feed.py's
docstring) -- this is the closest thing to a live-game rehearsal available
on a non-Windows dev box, and is what catches interaction bugs a
single-frame golden test (test_ocr_pipeline.py) can't: OCR on digits that
change from frame to frame.
"""
import pytest

from demo_feed import DemoExpFeed
from maple_analyzer.ocr import StatPanelOcr
from maple_analyzer.parser import parse_fields

from conftest import SAMPLE_IMAGE


@pytest.fixture(scope="module")
def ocr_engine():
    return StatPanelOcr()


def test_exp_increases_across_ticks(ocr_engine):
    feed = DemoExpFeed(SAMPLE_IMAGE, start_exp=100000, exp_per_tick=(50, 100))
    readings = []
    for _ in range(5):
        fields = feed.grab_fields()
        field_text = {name: ocr_engine.read_field(img, name) for name, img in fields.items()}
        snap = parse_fields(field_text)
        assert snap.exp_cur is not None, f"OCR failed to read EXP from synthetic frame: {field_text}"
        readings.append(snap.exp_cur)

    assert readings == sorted(readings), f"EXP should be non-decreasing across ticks: {readings}"
    assert readings[-1] > readings[0]
