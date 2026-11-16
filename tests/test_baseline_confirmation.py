"""Calibration: establishing max HP/MP and the starting EXP from scratch.

Session(require_calibration=True) is the default -- see rate.py's Session
docstring for the full design. In short: a single first reading is never
trusted (see the two bugs it fixes below), each of max HP, max MP, and
starting EXP is corroborated independently, and a tick where the whole
snapshot visibly moved counts for more than one where it didn't (the
liveness tiebreaker) -- but repetition alone is never sufficient, because
static OCR garbage from a covered panel repeats identically every tick too.
"""
from __future__ import annotations

from maple_analyzer.parser import StatSnapshot, parse_fields
from maple_analyzer.rate import Session

from captured_frames import COVERED_PANEL_FRAMES


def _tick(s, *, exp=None, exp_pct=None, hp=None, hp_max=None, mp=None, mp_max=None, level=None):
    s.record(exp_cur=exp, hp_cur=hp, mp_cur=mp, exp_pct=exp_pct, hp_max=hp_max, mp_max=mp_max, level=level)


# --- the two bugs this fixes --------------------------------------------


def test_a_garbage_first_max_does_not_lock_out_the_real_one():
    """Live capture, 2026-08-17: '2816' misread as '281616' on an early
    frame installed a bogus max that MAX_CHANGE_FACTOR then made permanent --
    the real 2816 is >100x away from 281616, so it could never be re-adopted.
    Calibration must not let a lone first reading become that max at all."""
    s = Session()
    _tick(s, mp=28163, mp_max=281616, hp=500, hp_max=824, exp=1000, exp_pct=10.0, level=44)
    assert s.is_calibrating
    # The real value follows and must win -- nothing was ever locked in.
    for _ in range(3):
        _tick(s, mp=1663, mp_max=2816, hp=500, hp_max=824, exp=1000, exp_pct=10.0, level=44)
    assert not s.is_calibrating
    assert s._mp._max == 2816


def test_a_garbage_first_exp_does_not_become_the_permanent_baseline():
    s = Session()
    _tick(s, exp=101_332_182, hp=500, hp_max=824, mp=1663, mp_max=2816, level=44)  # no pct -- structurally weak
    assert s.is_calibrating
    for exp in (10_133, 10_193, 10_253):
        _tick(s, exp=exp, exp_pct=2.16, hp=500, hp_max=824, mp=1663, mp_max=2816, level=44)
    assert not s.is_calibrating
    assert s.start_exp == 10_133  # the first of the corroborating streak, not the garbage


# --- repetition alone must not certify (the trap two earlier designs hit) --


def test_repetition_alone_never_confirms_faster_than_the_minimum():
    """The trap that sank two earlier LossTracker designs (see its
    docstring and mp-loss-investigation-2026-08-17.md): when a window covers
    the panel the OCR garbage is *static*, identical every tick, so
    N-identical-reads certifies it exactly as happily as it certifies the
    truth. The liveness tiebreaker only ever *shortens* confirmation for
    ticks where something moved; a frozen stream gets no discount and must
    still take the full CALIB_TARGET ticks, never fewer."""
    s = Session()
    for _ in range(Session.CALIB_TARGET - 1):
        _tick(s, mp=28163, mp_max=281616, hp=500, hp_max=824, exp=1000, exp_pct=10.0, level=44)
    assert s.is_calibrating  # one tick short of CALIB_TARGET -- must not have confirmed yet
    # ...and it does still complete: an idle character moves nothing either.
    _tick(s, mp=28163, mp_max=281616, hp=500, hp_max=824, exp=1000, exp_pct=10.0, level=44)
    assert not s.is_calibrating


def test_calibration_locks_onto_the_truth_before_real_garbage_arrives():
    """Replays the actual live capture behind the MP-loss investigation:
    3 clean frames, then the panel gets covered and OCR turns to
    structurally unparseable text (no 'HP'/'MP' prefix at all -- see
    parser.py) for the rest of the run. Calibration must lock onto the real
    max/baseline from the clean lead-in and hold it; test_captured_regression.py
    separately proves the post-calibration guards then survive the garbage
    that follows -- this test is only about getting to a good baseline in
    the first place."""
    s = Session()
    last = StatSnapshot(None, None, None, None, None, None, None)
    for frame in COVERED_PANEL_FRAMES:
        snap = parse_fields(frame)
        merged = StatSnapshot(*(
            new if new is not None else old
            for new, old in zip(vars(snap).values(), vars(last).values())
        ))
        last = merged
        s.record(
            merged.exp_cur, merged.hp_cur, merged.mp_cur, merged.exp_pct,
            hp_max=merged.hp_max, mp_max=merged.mp_max, level=merged.level,
        )
        if not s.is_calibrating:
            break
    assert not s.is_calibrating
    assert s._hp._max == 824
    assert s._mp._max == 2816


# --- the liveness tiebreaker: live ticks confirm faster ------------------


def test_live_ticks_confirm_in_two():
    s = Session()
    _tick(s, hp=800, hp_max=824, mp=2800, mp_max=2816, exp=100, exp_pct=0.02, level=44)
    assert s.is_calibrating
    _tick(s, hp=800, hp_max=824, mp=2800, mp_max=2816, exp=200, exp_pct=0.04, level=44)
    assert not s.is_calibrating  # EXP moving drives liveness for HP/MP's calibration too


# --- while calibrating, nothing is recorded -------------------------------


def test_nothing_is_recorded_until_calibration_completes():
    s = Session()
    _tick(s, hp=800, hp_max=824, mp=2800, mp_max=2816, exp=100, exp_pct=0.02, level=44)
    assert s.start_exp is None
    assert s.exp_diff is None
    assert s.hp_loss == 0
    assert s.elapsed() == 0.0


def test_a_permanently_obscured_panel_records_nothing_ever():
    """Documented consequence: if the panel is covered from the very first
    frame, there is no clean lead-in for calibration to lock onto (unlike
    test_calibration_locks_onto_the_truth_before_real_garbage_arrives, where
    3 good frames come first) and it never completes -- the session records
    nothing, rather than silently recording garbage as it used to. Uses
    genuinely unparseable text (no 'HP'/'MP' prefix), matching what a
    covered panel actually produces per parser.py -- every field OCR's to
    None every tick, so there is nothing for calibration to even count."""
    s = Session()
    for _ in range(50):
        s.record(exp_cur=None, hp_cur=None, mp_cur=None, hp_max=None, mp_max=None, level=None)
    assert s.is_calibrating
    assert s.start_exp is None


# --- once confirmed, the confirming tick's own reading still counts -------


def test_the_confirming_tick_is_not_wasted():
    s = Session()
    _tick(s, hp=800, hp_max=824, mp=2800, mp_max=2816, exp=100, exp_pct=0.02, level=44)
    _tick(s, hp=800, hp_max=824, mp=2800, mp_max=2816, exp=200, exp_pct=0.04, level=44)  # confirms here
    assert not s.is_calibrating
    assert s.exp_diff == 100  # 200 - 100, not 0 -- this tick's own gain isn't thrown away


# --- calibration only ever happens once per Session's lifetime -----------


def test_restart_after_calibration_does_not_recalibrate():
    s = Session()
    _tick(s, hp=800, hp_max=824, mp=2800, mp_max=2816, exp=100, exp_pct=0.02, level=44)
    _tick(s, hp=800, hp_max=824, mp=2800, mp_max=2816, exp=200, exp_pct=0.04, level=44)
    assert not s.is_calibrating
    s.start()  # restart -- must not re-enter calibration
    assert not s.is_calibrating
    assert s.start_exp == 200  # carried forward instantly, no recalibration wait
    _tick(s, hp=800, hp_max=824, mp=2800, mp_max=2816, exp=250, exp_pct=0.05, level=44)
    assert s.exp_diff == 50


# --- require_calibration=False is the exact pre-calibration behaviour ----


def test_calibration_can_be_disabled_for_tests_that_predate_it():
    s = Session(require_calibration=False)
    assert not s.is_calibrating
    _tick(s, exp=1000, hp=500, mp=200)
    assert s.start_exp == 1000
    assert s.exp_diff == 0
