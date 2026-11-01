"""Integration smoke test: DemoExpFeed -> real OCR -> real parser, several
ticks in a row. Only the "camera" is fake here (see demo_feed.py's
docstring) -- this is the closest thing to a live-game rehearsal available
on a non-Windows dev box, and is what catches interaction bugs a
single-frame golden test (test_ocr_pipeline.py) can't, e.g. a redrawn EXP
region overlapping a neighboring field's crop box.
"""
import pytest

from maple_analyzer.demo_feed import DemoExpFeed
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


def test_other_fields_unaffected_by_synthetic_exp_redraw(ocr_engine):
    # DemoExpFeed only redraws the EXP value text -- LV/HP/MP should read
    # identically to the static golden values every tick.
    feed = DemoExpFeed(SAMPLE_IMAGE)
    fields = feed.grab_fields()
    field_text = {name: ocr_engine.read_field(img, name) for name, img in fields.items()}
    snap = parse_fields(field_text)
    assert (snap.hp_cur, snap.hp_max) == (373, 922)
    assert (snap.mp_cur, snap.mp_max) == (723, 3290)
