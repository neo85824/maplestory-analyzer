"""EXP accounting across a level-up.

The game's EXP counter resets to ~0 at every level, so `end - start` -- which
is what Session.exp_diff did -- turns a real gain into a large negative the
moment a session spans a level-up. Observed live 2026-08-18: a genuine ~48k
gain rendered as "+-379,946" in the HUD (the '+' is hardcoded in _render).

A bare EXP *drop* is ambiguous: it is either a level-up or an OCR misread, and
misreads are known to happen (see loss.py's LossTracker). The level is what
disambiguates -- EXP dropping while the level rises is a level-up; EXP
dropping with the level unchanged is noise and must not be counted.
"""
from __future__ import annotations

import pytest

from captured_levelup_frames import LEVELUP_FRAMES
from maple_analyzer.parser import StatSnapshot, parse_fields
from maple_analyzer.rate import Session

# Derived from the live capture: level 44's total EXP is cur/(pct/100) at the
# last reading before the level-up, 428194 / 0.9998.
LV44_TOTAL = 428194 / 0.9998


def _record(session, *, exp, pct=None, level=None, hp=800, mp=2800, hp_max=824, mp_max=2816):
    session.record(
        exp_cur=exp, hp_cur=hp, mp_cur=mp, exp_pct=pct, level=level,
        hp_max=hp_max, mp_max=mp_max,
    )


# --- the reported case -------------------------------------------------


def test_gain_is_positive_and_correct_across_a_level_up():
    s = Session(require_calibration=False)
    s.start()
    _record(s, exp=380_000, pct=88.72, level=44)
    _record(s, exp=428_194, pct=99.98, level=44)
    _record(s, exp=54, pct=0.01, level=45, hp_max=838, mp_max=2882)

    # finished off level 44 (total - start), then 54 into level 45
    expected = (LV44_TOTAL - 380_000) + 54
    assert s.exp_diff > 0
    assert abs(s.exp_diff - expected) < 100, s.exp_diff


def test_exp_diff_is_never_negative_after_a_level_up():
    """The visible symptom: _render prints exp_diff behind a hardcoded '+'."""
    s = Session(require_calibration=False)
    s.start()
    _record(s, exp=428_194, pct=99.98, level=44)
    _record(s, exp=54, pct=0.01, level=45)
    assert s.exp_diff >= 0


def test_summary_reports_the_gain_not_end_minus_start():
    s = Session(require_calibration=False)
    s.start()
    _record(s, exp=380_000, pct=88.72, level=44)
    _record(s, exp=428_194, pct=99.98, level=44)
    _record(s, exp=54, pct=0.01, level=45)
    summary = s.finalize(interval_minutes=10)
    assert summary.exp_diff > 0
    # start/end stay raw and honest about what was on screen
    assert summary.start_exp == 380_000
    assert summary.end_exp == 54


# --- must not be confused with noise -----------------------------------


def test_exp_drop_without_a_level_change_is_ignored():
    """An OCR misread, not a level-up. Counting it would inflate the gain by
    a whole level every time EXP is misread low."""
    s = Session(require_calibration=False)
    s.start()
    _record(s, exp=400_000, pct=93.4, level=44)
    _record(s, exp=4, pct=0.01, level=44)      # misread, level unchanged
    _record(s, exp=400_100, pct=93.42, level=44)
    assert s.exp_diff == 100


# --- other scenarios ---------------------------------------------------


def test_two_level_ups_in_one_session():
    s = Session(require_calibration=False)
    s.start()
    _record(s, exp=400_000, pct=93.4, level=44)     # lv44 total ~428,265
    _record(s, exp=20, pct=0.004, level=45)         # ding
    _record(s, exp=500_000, pct=92.6, level=45)     # lv45 total ~540,000
    _record(s, exp=10, pct=0.002, level=46)         # ding again
    assert s.exp_diff > 500_000
    assert s.exp_diff < 700_000


def test_level_up_with_no_percentage_reading_does_not_produce_nonsense():
    """exp_pct is what derives the level's total EXP; without it the finished
    level's remainder is unknowable. Under-count, never invent."""
    s = Session(require_calibration=False)
    s.start()
    _record(s, exp=400_000, pct=None, level=44)
    _record(s, exp=54, pct=None, level=45)
    assert s.exp_diff >= 0
    assert s.exp_diff < 400_000  # no fabricated total


def test_restart_after_a_level_up_baselines_cleanly():
    s = Session(require_calibration=False)
    s.start()
    _record(s, exp=428_194, pct=99.98, level=44)
    _record(s, exp=54, pct=0.01, level=45)
    s.start()  # user hits Restart Session
    assert s.exp_diff in (0, None)
    _record(s, exp=154, pct=0.03, level=45)
    assert s.exp_diff == 100


# --- against the real captured frames ----------------------------------


def test_real_captured_level_up_frames():
    """41 verbatim frames spanning the live LV44->45 transition, driven the
    way overlay._do_tick drives them (carry forward missing fields)."""
    s = Session(require_calibration=False)
    s.start()
    last = StatSnapshot(None, None, None, None, None, None, None)
    for frame in LEVELUP_FRAMES:
        snap = parse_fields(frame)
        merged = StatSnapshot(*(
            new if new is not None else old
            for new, old in zip(vars(snap).values(), vars(last).values())
        ))
        last = merged
        s.record(
            merged.exp_cur, merged.hp_cur, merged.mp_cur, merged.exp_pct,
            level=merged.level, hp_max=merged.hp_max, mp_max=merged.mp_max,
        )
    # the window starts at ~428,124 and ends 54 into the next level, so the
    # gain across it is small and positive -- never the -428k of the old math
    assert 0 <= s.exp_diff < 1000, s.exp_diff


# --- stability: the reason this is segments, not an accumulator ------------


def test_garbage_cannot_accumulate_over_many_ticks():
    """The regression that killed the tick-by-tick accumulator: one frame read
    'EXP101332182' and booked +101,322,049 that never came back, because
    summing rises bakes in any absurd value forever. Each tick is measured
    from the segment start instead, so repeated garbage cannot compound. (A
    single garbage frame being rejected outright is test_exp_garbage.py.)"""
    s = Session(require_calibration=False)
    s.start()
    _record(s, exp=1000, pct=1.0, level=45)
    for _ in range(50):
        _record(s, exp=99_999_999, pct=None, level=45)
        _record(s, exp=1000, pct=1.0, level=45)
    assert s.exp_diff == 0


def test_gained_percentage_adds_up_level_by_level():
    """The last 40% of one level plus the first 5% of the next is 45% of a
    level. Dividing the whole gain by the new level's total -- what the HUD
    and History did -- gave (400 + 100) / 2000 = 25%."""
    s = Session(require_calibration=False)
    for cur in (600, 700, 800, 900):
        s.record(cur, 100, 100, cur / 10, level=10)       # level 10 needs 1,000
    for cur in (20, 60, 100):
        s.record(cur, 100, 100, cur / 20, level=11)       # level 11 needs 2,000
    assert s.exp_diff == 400 + 100
    assert s.exp_pct_gained == pytest.approx(45.0)
    summary = s.finalize(10)
    assert summary.exp_pct_diff == pytest.approx(45.0)
