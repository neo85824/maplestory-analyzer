"""Rejecting garbage EXP readings at the session.

Three layers, each covering the previous one's blind spot:

1. parser  -- the game renders EXP as `cur[pct%]`, so a read with no opening
   bracket is structurally wrong and must not yield a number at all
   (tests/test_parser.py).
2. session -- `cur` and `pct` are independent readings of the same quantity,
   so their ratio (the level's total EXP) cross-checks them.
3. rate.py's segments -- whatever still slips through is transient, because
   the gain is measured from a baseline rather than accumulated.

Layer 2 exists mainly to protect `finalize()`: segments self-correct between
ticks, but a summary freezes one instant, and a garbage frame captured there
is written to History permanently.

Every string below is verbatim from a live capture.
"""
from __future__ import annotations

from maple_analyzer.rate import Session


# --- layer 2: the cur/pct cross-check ------------------------------------


def _rec(s, exp, pct, level=45):
    s.record(exp_cur=exp, hp_cur=500, mp_cur=1000, exp_pct=pct, level=level,
             hp_max=838, mp_max=2882)


def test_reading_inconsistent_with_its_own_percentage_is_rejected():
    """'EXP S255[1 12%]': the leading 5 read as an S, so cur=255 at 1.12%
    implies a level total of 22,768 against an established ~468,500."""
    s = Session(require_calibration=False); s.start()
    _rec(s, 5255, 1.12)
    _rec(s, 5435, 1.16)
    before = s.exp_diff
    _rec(s, 255, 1.12)          # the misread
    assert s.exp_diff == before  # carried forward, not counted


def test_huge_jump_without_a_percentage_is_rejected():
    """No pct to cross-check against, so the bound is that a single 500ms tick
    cannot move EXP by more than a whole level."""
    s = Session(require_calibration=False); s.start()
    _rec(s, 10133, 2.16)
    _rec(s, 10173, 2.17)
    _rec(s, 101332182, None)
    assert s.exp_diff == 40
    # ...and the case segments alone cannot fix: the session timer firing on
    # that frame used to write it to History permanently.
    assert s.finalize(interval_minutes=10).exp_gained == 40


def test_normal_readings_are_not_rejected():
    s = Session(require_calibration=False); s.start()
    for exp, pct in ((5255, 1.12), (5435, 1.16), (5495, 1.17), (52893, 11.29)):
        _rec(s, exp, pct)
    assert s.exp_diff == 52893 - 5255


def test_tiny_percentages_are_not_falsely_rejected():
    """pct is rounded to 2dp, so just after a level-up the implied total is
    numerically unstable -- 54/0.0001 is a 50%-uncertain estimate. A fixed
    band would reject every legitimate reading there."""
    s = Session(require_calibration=False); s.start()
    s.record(exp_cur=428194, hp_cur=500, mp_cur=1000, exp_pct=99.98, level=44,
             hp_max=838, mp_max=2882)
    _rec(s, 54, 0.01, level=45)      # level-up: re-baselines
    _rec(s, 254, 0.05, level=45)
    assert s.exp_diff > 0


def test_first_reading_is_always_accepted():
    s = Session(require_calibration=False); s.start()
    _rec(s, 5255, 1.12)
    assert s.exp_diff == 0
    _rec(s, 5355, 1.14)
    assert s.exp_diff == 100
