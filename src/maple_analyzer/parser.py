"""Turn per-field OCR text (from regions.py's field_boxes() + ocr.py's
read_field()) into structured HP/MP/EXP/LV values.

Each field is OCR'd from its own tightly-cropped, isolated box now (see the
2026-08-17 recognition-only rework in ocr.py/capture.py) -- there's exactly
one string per field, always. Earlier versions of this module had to handle
RapidOCR sometimes merging a label+value into one detected box and sometimes
splitting them into two ('HP' + '[506/824]'), with a position-based
nearest-neighbor fallback for the split case; that's gone now, because
per-field cropping means there's no "other box" to merge or split against --
regex against the one string is always enough.

EXP is shown by the game as `cur[percentage%]` together, e.g. `162950[38.05%]`.
The '.' and/or closing ']' are the most OCR-fragile part of the string (observed
dropped in testing, e.g. '4980%' instead of '49.80%') -- normalized by treating
a bare >=3-digit run before '%' as implying 2 decimal places, since that's this
game's percentage precision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_PAIR_RE = {
    "HP": re.compile(r"HP\D{0,3}(\d+)\D+(\d+)", re.IGNORECASE),
    "MP": re.compile(r"MP\D{0,3}(\d+)\D+(\d+)", re.IGNORECASE),
}
# The game renders EXP as `cur[pct%]`, so the opening bracket is structure,
# not decoration: a read without one is broken and must not yield a number.
# Measured over 12,384 live reads, requiring it costs 0.4% -- and those are
# garbage like 'EXP101332182' (booked +101,322,049 of phantom gain before this)
# and 'EXP357041183.37%]', where the missing bracket merged 357041 and 183 into
# one number. OCR reads '[' as '(' or '{' often enough to accept those too.
_EXP_CUR_RE = re.compile(r"EXP\D{0,3}(\d+)\s*[\[({]", re.IGNORECASE)
# Percentage is 0-99.99. Separator between the two digit groups is normally
# '.', but OCR sometimes drops it entirely (bare 3-4 digit run) or -- seen
# with recognition-only OCR on this tiny font -- reads it as a space or a
# colon instead ('63 14%', '75:11%'). The colon form showed up in 37 of 235
# ticks in a live capture (2026-08-17), each one silently costing exp_pct and
# with it the EXP% display and the level-up ETA. All forms captured here;
# _normalize_pct interprets them. A bare 1-2 digit run is deliberately NOT
# matched, ambiguous with stray adjacent OCR noise.
#
# Tightened 2026-11-17 after a 29-minute capture on the new HUD, in three ways
# that all turn a *wrong* percentage into a missing one (which rate.py and the
# HUD can fill in from the level total -- a wrong one they would have shown):
#
# - The whole run between the bracket and the '%' must be the percentage. It
#   used to be searched for anywhere after the bracket, so '[11 738%]' (an
#   extra digit; really 11.78) matched its tail and came out as 7.38.
# - Exactly two decimals. The game always prints two, so '[11 2%]' has lost a
#   digit (really 11.32) and is not 11.2.
# - More separators: the decimal point also arrives as ',', ' .', ' -', ':-'
#   and similar. '[12,19%]' used to yield no percentage at all.
_EXP_PCT_RE = re.compile(r"\s*(\d{1,2}[.\s:,;\-]{1,3}\d{2}|\d{3,4})\s*%")
_LV_RE = re.compile(r"LV\.?\D{0,3}(\d+)", re.IGNORECASE)


@dataclass
class StatSnapshot:
    level: int | None
    hp_cur: int | None
    hp_max: int | None
    mp_cur: int | None
    mp_max: int | None
    exp_cur: int | None
    exp_pct: float | None


def _normalize_pct(raw: str) -> float:
    # Recognition-only OCR on the tiny EXP field font sometimes reads the
    # decimal point as a space ('63 14%') or a colon ('75:11%') rather than
    # dropping it outright -- treat both the same as a dot.
    raw = re.sub(r"[.\s:,;\-]+", ".", raw)
    if "." in raw:
        return float(raw)
    if len(raw) > 2:
        return float(f"{raw[:-2]}.{raw[-2:]}")
    return float(raw)


def _find_pair(label: str, text: str) -> tuple[int | None, int | None]:
    m = _PAIR_RE[label].search(text)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _find_exp(text: str) -> tuple[int | None, float | None]:
    m = _EXP_CUR_RE.search(text)
    if not m:
        return None, None
    cur = int(m.group(1))
    pm = _EXP_PCT_RE.match(text, m.end())
    pct = _normalize_pct(pm.group(1)) if pm else None
    return cur, pct


def _find_level(text: str) -> int | None:
    m = _LV_RE.search(text)
    return int(m.group(1)) if m else None


def parse_fields(field_text: dict[str, str]) -> StatSnapshot:
    """field_text: {'LV': ..., 'HP': ..., 'MP': ..., 'EXP': ...} -- the raw
    recognized text for each of regions.py's field_boxes()."""
    hp_cur, hp_max = _find_pair("HP", field_text.get("HP", ""))
    mp_cur, mp_max = _find_pair("MP", field_text.get("MP", ""))
    exp_cur, exp_pct = _find_exp(field_text.get("EXP", ""))
    level = _find_level(field_text.get("LV", ""))
    return StatSnapshot(
        level=level,
        hp_cur=hp_cur, hp_max=hp_max,
        mp_cur=mp_cur, mp_max=mp_max,
        exp_cur=exp_cur, exp_pct=exp_pct,
    )
