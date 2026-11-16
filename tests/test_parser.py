"""parser.py: field text -> StatSnapshot. Pure regex/normalisation -- no OCR,
no images. Every EXP string is verbatim from a live capture unless it is
obviously a constructed minimal case.

The EXP field is where the care goes. The game prints it as `cur[pct%]`, and
the rules below all come from one principle: a reading that does not look
like that yields *nothing* rather than a wrong number, because the overlay
carries the last good value forward for an unreadable field, while a wrong
number gets counted.
"""
import pytest

from maple_analyzer.parser import parse_fields


def test_clean_fields():
    snap = parse_fields({
        "LV": "LV.44", "HP": "HP[377/824]", "MP": "MP[1663/2816]", "EXP": "EXP162950[38.05%]",
    })
    assert snap.level == 44
    assert (snap.hp_cur, snap.hp_max) == (377, 824)
    assert (snap.mp_cur, snap.mp_max) == (1663, 2816)
    assert snap.exp_cur == 162950
    assert snap.exp_pct == 38.05


def test_missing_field_is_none():
    snap = parse_fields({})
    assert snap.level is None
    assert snap.hp_cur is None and snap.hp_max is None
    assert snap.mp_cur is None and snap.mp_max is None
    assert snap.exp_cur is None and snap.exp_pct is None


def test_garbage_text_does_not_raise():
    snap = parse_fields({"LV": "??", "HP": "", "MP": "garbage###", "EXP": "not an exp string"})
    assert snap.level is None
    assert snap.hp_cur is None
    assert snap.mp_cur is None
    assert snap.exp_cur is None


def test_hp_mp_case_insensitive_label():
    snap = parse_fields({"HP": "hp[10/20]", "MP": "mp[5/30]"})
    assert (snap.hp_cur, snap.hp_max) == (10, 20)
    assert (snap.mp_cur, snap.mp_max) == (5, 30)


# --- EXP: the decimal point arrives in many shapes ------------------------


@pytest.mark.parametrize("raw,cur,pct", [
    ("EXP 252463[33.72%]", 252463, 33.72),
    ("EXP162950[3805%]", 162950, 38.05),        # dropped: a bare 3-4 digit run means 2 decimals
    ("EXP 50355[638%] ", 50355, 6.38),
    ("EXP 322928[75 40%]", 322928, 75.40),      # read as a space
    ("EXP 5435[1 24%] ", 5435, 1.24),
    ("EXP 321675[75:11%] ", 321675, 75.11),     # a colon -- 37 of 235 ticks in one capture
    ("EXP 96243[12,19%]", 96243, 12.19),        # a comma
    ("EXP 328520[76 .71%]", 328520, 76.71),     # space-dot
    ("EXP 321234[75 -01%] ", 321234, 75.01),    # space-dash
    ("EXP 321234[75:-01%]", 321234, 75.01),
])
def test_percentage_separator_variants(raw, cur, pct):
    snap = parse_fields({"EXP": raw})
    assert (snap.exp_cur, snap.exp_pct) == (cur, pct)


def test_bracket_confusions_are_tolerated():
    """OCR reads '[' as '(' or '{' often enough that rejecting those would
    throw away good frames."""
    snap = parse_fields({"EXP": "EXP 404715(94 50%]"})
    assert (snap.exp_cur, snap.exp_pct) == (404715, 94.50)


# --- EXP: a malformed percentage is missing, not wrong --------------------


@pytest.mark.parametrize("raw,cur,was", [
    ("EXP 93044[11 738%]", 93044, 7.38),   # extra digit; really 11.78
    ("EXP 89361[11 2%]", 89361, 11.2),     # lost a digit; really 11.32
    ("EXP 60960[7 2%] ", 60960, 7.2),      # really 7.72
    ("EXP 94445[1 936%]", 94445, 9.36),    # really 11.96
    ("EXP162950[5%]", 162950, None),       # a bare 1-2 digit run is ambiguous
])
def test_malformed_percentage_is_missing_not_wrong(raw, cur, was):
    """The value is still read; the level total supplies the percentage
    (exp_total.py). The last column is what these used to parse to."""
    snap = parse_fields({"EXP": raw})
    assert snap.exp_cur == cur
    assert snap.exp_pct is None, f"used to be {was}"


# --- EXP: no opening bracket, no number -----------------------------------


@pytest.mark.parametrize("raw,would_have_been", [
    ("EXP101332182", 101332182),              # booked +101,322,049 as gain
    ("EXP162950 3805%", 162950),
    ("EXP 128832 75%] ", 128832),             # no '[', and the cross-check misses it
    ("EXP526784178 8071 11819", 526784178),
    ("EXP357041183.37%]", 357041183),         # missing '[' merged digits together
    ("EXP629082 76 84716261", 629082),
    ("EXP 21410052 ", 21410052),
    ("EXP3691 94[86.20%]", 3691),             # digits split; old code took the first run
])
def test_structurally_broken_readings_are_unreadable(raw, would_have_been):
    """The bracket is structure, not decoration. Treating it as optional is
    what let 'EXP357041183.37%]' become 357,041,183. Requiring it costs 0.4%
    of reads (51 of 12,384 measured live), nearly all of them garbage. The
    percentage goes with the value: if the field does not look like
    `cur[pct%]`, neither number in it is trustworthy."""
    snap = parse_fields({"EXP": raw})
    assert snap.exp_cur is None
    assert snap.exp_pct is None
