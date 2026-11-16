"""The level's total EXP by consensus, and what it protects.

Every percentage the HUD shows -- EXP gained %, projected %, the level-up ETA,
the EXP bar -- comes from the level's total EXP, which the game never prints:
it is cur / (pct/100). Up to v1.0.5 that was taken from whichever tick was
being rendered, so a misread tick put a wrong percentage on screen. In the
29-minute live capture replayed below, 387 of 3,408 ticks showed a wrong EXP
row and 352 a derived percentage off by more than 1% (273 of them by more
than half).

exp_total.py's LevelTotalEstimator establishes the total from many readings
instead. The unit tests pin down the properties that make that safe; the
replay tests run the real captured text through OverlayApp._do_tick.

Every EXP string here is verbatim from a live capture.
"""
from __future__ import annotations

import pytest

from captured_exp_frames import NEW_HUD_29MIN, NO_BRACKET_MERGE, STARTS_ON_A_MISREAD
from maple_analyzer.exp_total import LevelTotalEstimator
from maple_analyzer.parser import parse_fields
from maple_analyzer.rate import Session
from test_run_state import _StubApp

TOTAL = 789_628  # level 52, fitted from the capture (see captured_exp_frames.py)


def _pct(cur: int, total: int = TOTAL) -> float:
    return round(cur / total * 100, 2)


def _established(start: int = 50_000, step: int = 150, n: int = 8) -> tuple[LevelTotalEstimator, int]:
    """An estimator fed n good readings; returns it and the last cur."""
    est = LevelTotalEstimator()
    cur = start
    for i in range(n):
        cur = start + i * step
        est.observe(cur, _pct(cur), 52)
    assert est.total is not None
    return est, cur


# --- the parser: a wrong percentage becomes a missing one ----------------


@pytest.mark.parametrize("raw,cur,pct", [
    ("EXP 50355[638%] ", 50355, 6.38),          # separator dropped
    ("EXP 50535[6 40%] ", 50535, 6.40),         # ...read as a space
    ("EXP 61426[13:11%]", 61426, 13.11),        # ...a colon
    ("EXP 96243[12,19%]", 96243, 12.19),        # ...a comma (was: no pct at all)
    ("EXP 328520[76 .71%]", 328520, 76.71),     # ...space-dot
    ("EXP 321234[75 -01%] ", 321234, 75.01),    # ...space-dash
    ("EXP 321234[75:-01%]", 321234, 75.01),
    ("EXP 252463[33.72%]", 252463, 33.72),
])
def test_percentage_separator_variants(raw, cur, pct):
    snap = parse_fields({"EXP": raw})
    assert (snap.exp_cur, snap.exp_pct) == (cur, pct)


@pytest.mark.parametrize("raw,cur,was", [
    ("EXP 93044[11 738%]", 93044, 7.38),   # extra digit; really 11.78
    ("EXP 89361[11 2%]", 89361, 11.2),     # lost a digit; really 11.32
    ("EXP 60960[7 2%] ", 60960, 7.2),      # really 7.72
    ("EXP 94445[1 936%]", 94445, 9.36),    # really 11.96
])
def test_malformed_percentage_is_missing_not_wrong(raw, cur, was):
    """Each of these used to parse to the wrong percentage in the last
    column. The value is still read -- the level total supplies the rest."""
    snap = parse_fields({"EXP": raw})
    assert snap.exp_cur == cur
    assert snap.exp_pct is None, f"used to be {was}"


# --- the estimator --------------------------------------------------------


def test_no_opinion_before_a_total_is_established():
    est = LevelTotalEstimator()
    assert est.observe(50_355, 6.38, 52) is None
    assert est.total is None
    assert est.reading == (50_355, 6.38)  # still remembered as the latest


def test_repetition_alone_never_establishes_a_total():
    """A covered panel or an idle character repeats one text every tick. The
    live case: 'EXP S255[1 12%]' on 379 of 429 ticks."""
    est = LevelTotalEstimator()
    for _ in range(1000):
        est.observe(255, 1.12, 45)
    assert est.total is None


def test_distinct_agreeing_readings_establish_the_total():
    est, _ = _established()
    assert est.total == pytest.approx(TOTAL, rel=0.002)


def test_a_narrow_run_of_readings_is_not_enough():
    """Three readings inside 0.05 of a percent could be a systematic misread
    agreeing with itself (next test), so they do not count yet."""
    est = LevelTotalEstimator()
    for cur in (93_398, 93_420, 93_450, 93_480):
        est.observe(cur, _pct(cur), 52)
    assert est.total is None


def test_a_dropped_leading_digit_cannot_certify_itself():
    """'11.83' read as '1.83', reading after reading. Neighbouring wrong
    totals are close enough to agree pairwise, which is exactly what a naive
    'N readings agree' rule would accept. From the live capture."""
    est = LevelTotalEstimator()
    for cur, pct in [
        (93398, 1.83), (93501, 1.84), (93557, 1.85), (93601, 1.85), (93922, 1.89),
        (94401, 1.96), (95100, 1.04), (89096, 1.28), (89659, 1.35), (89830, 1.38),
        (89864, 1.38), (91135, 1.54), (91179, 1.55), (86330, 1.93), (86500, 1.95),
    ]:
        est.observe(cur, pct, 52)
    assert est.total is None


@pytest.mark.parametrize("cur,pct,what", [
    (98700, 1.50, "pct lost a digit (12.50)"),
    (92489, 11.21, "7 read as 2 (11.71)"),
    (9991, 12.66, "cur lost a digit (99991)"),
    (901659, 11.42, "cur gained a digit (90165)"),
    (10002, 12.67, "cur lost a digit (100020)"),
])
def test_misreads_are_rejected_once_established(cur, pct, what):
    est, last = _established(start=85_000)
    assert est.observe(cur, pct, 52) is False, what
    assert est.reading[0] == last  # the last good reading stays up


def test_a_reading_that_lost_a_digit_from_both_numbers_is_rejected():
    """'8721[111%]' for 87210[11.04%]: the same ratio, so it agrees with the
    total. What gives it away is that EXP cannot fall within a level."""
    est, last = _established(start=87_000)
    assert est.agrees(8721, 1.11)
    assert est.observe(8721, 1.11, 52) is False
    assert est.observe(8721, 1.11, 52) is False
    assert est.reading[0] == last


def test_a_wrongly_high_reading_does_not_lock_out_the_truth():
    """The other side of the previous test: if the reading that was wrong was
    the earlier, higher one, the lower readings that follow are the truth and
    must win -- after a few distinct ones have said so."""
    est, last = _established(start=60_000)
    est._last_cur = 600_000  # as if a bad high reading had been trusted
    verdicts = []
    for i in range(1, 5):
        cur = last + i * 200
        verdicts.append(est.observe(cur, _pct(cur), 52))
    assert verdicts[-1] is True
    assert est.reading[0] == last + 4 * 200


def test_a_reading_without_a_percentage_waits_then_is_bounded():
    est, last = _established(start=60_000)
    assert est.observe(last + 100, None, 52) is False      # unverifiable: skipped
    for _ in range(est.NO_PCT_PATIENCE):
        est.observe(last + 100, None, 52)
    assert est.observe(last + 200, None, 52) is True       # pct is not coming back
    assert est.reading[0] == last + 200
    assert est.reading[1] == pytest.approx(_pct(last + 200), abs=0.02)  # filled in from the total
    assert est.observe(last - 5_000, None, 52) is False    # below the last good reading
    assert est.observe(TOTAL * 2, None, 52) is False       # past the level's total


def test_level_up_starts_a_new_total():
    est, _ = _established(start=780_000, step=100)
    assert est.observe(120, 0.01, 53) is None  # reset: no opinion again
    assert est.total is None
    new_total = 850_000
    for cur in range(1_000, 9_000, 800):
        est.observe(cur, _pct(cur, new_total), 53)
    assert est.total == pytest.approx(new_total, rel=0.01)


def test_a_level_misread_alone_does_not_discard_the_total():
    est, last = _established()
    before = est.total
    assert est.observe(last + 100, _pct(last + 100), 57) is True  # LV misread, EXP fine
    assert est.observe(last + 200, _pct(last + 200), 52) is True
    assert est.total == pytest.approx(before, rel=0.001)


def test_a_missed_level_up_is_recovered_from():
    """LV unreadable throughout, so nothing announces the new level -- the
    old total has to be given up on the evidence of the EXP readings alone.
    Before this, the session rejected every reading until the app restarted."""
    est = LevelTotalEstimator()
    for i in range(10):
        cur = 780_000 + i * 150
        est.observe(cur, _pct(cur), None)
    new_total = 850_000
    verdicts = [
        est.observe(cur, _pct(cur, new_total), None) for cur in range(1_000, 13_000, 800)
    ]
    assert verdicts[-1] is True
    assert est.total == pytest.approx(new_total, rel=0.01)


# --- the session ----------------------------------------------------------


def _running_session(start: int = 85_000, n: int = 8, step: int = 150) -> tuple[Session, int]:
    s = Session(require_calibration=False)
    cur = start
    for i in range(n):
        cur = start + i * step
        s.record(cur, 800, 2800, _pct(cur), hp_max=936, mp_max=3360, level=52)
    return s, cur


def test_total_exp_does_not_move_with_one_misread_tick():
    """total_exp is what every HUD percentage divides by. '11 21%' for 11.71%
    is within the old 25% band, so it used to be accepted and to shift the
    total by 4% for as long as that reading stayed on screen."""
    s, last = _running_session(start=92_000)
    before = s.total_exp
    s.record(92489, 800, 2800, 11.21, hp_max=936, mp_max=3360, level=52)
    assert s.total_exp == pytest.approx(before, rel=0.001)
    assert s.total_exp == pytest.approx(TOTAL, rel=0.002)
    assert s.exp_reading[0] == last


def test_a_misread_final_tick_is_not_written_to_history():
    s, last = _running_session(start=87_000)
    gained = s.exp_diff
    s.record(8721, 800, 2800, 1.11, hp_max=936, mp_max=3360, level=52)  # both lost a digit
    summary = s.finalize(10)
    assert summary.exp_gained == gained
    assert summary.end_exp == last
    assert summary.total_exp == pytest.approx(TOTAL, rel=0.002)


def test_resuming_on_a_misread_tick_does_not_wipe_out_the_gain():
    """resume() used to re-anchor on whatever the first tick read. An extra
    digit there ('901659' for 90165) moved the baseline past anything the
    session could reach, and EXP gained read 0 for the rest of it."""
    s, last = _running_session(start=89_000)
    gained = s.exp_diff
    s.pause()
    s.resume()
    s.record(901659, 800, 2800, 11.42, hp_max=936, mp_max=3360, level=52)
    for i in range(1, 4):
        cur = last + i * 150
        s.record(cur, 800, 2800, _pct(cur), hp_max=936, mp_max=3360, level=52)
    # The first good reading after the resume is the re-anchor (nothing is
    # counted across the pause); the two after it are counted.
    assert s.exp_diff == gained + 300


def test_starting_from_a_synced_value_keeps_that_baseline():
    """Start after a stop baselines off the value on screen now (sync_current).
    The percentage the session still had on file belonged to the older value;
    paired with the new one it looked like a misread baseline and was thrown
    away once the level total was known, losing the first gains."""
    s = Session(require_calibration=False)
    s.record(50_000, 800, 2800, _pct(50_000), hp_max=936, mp_max=3360, level=52)
    s.pause()
    s.sync_current(70_000, 800, 2800)  # gained 20,000 while stopped
    s.start()
    for i in range(1, 9):
        cur = 70_000 + i * 150
        s.record(cur, 800, 2800, _pct(cur), hp_max=936, mp_max=3360, level=52)
    assert s.total_exp == pytest.approx(TOTAL, rel=0.002)
    assert s.start_exp == 70_000
    assert s.exp_diff == 8 * 150


# --- real captures, through the real tick loop ---------------------------


class _ReplaySource:
    def __init__(self, runs, hp="HP [621/936]", mp="MP [943/3360]"):
        self._ticks = ((lv, exp) for lv, exp, n in runs for _ in range(n))
        self._hp, self._mp = hp, mp

    def grab_fields(self):
        lv, exp = next(self._ticks)
        return {"LV": lv, "HP": self._hp, "MP": self._mp, "EXP": exp}


def _replay(runs):
    """Yield the app after each tick of a capture, tracking from the start."""
    app = _StubApp()
    app._source = _ReplaySource(runs)
    app._run_state = "running"
    app._settings.window_min = 10_000  # one session for the whole capture
    for _ in range(sum(n for _lv, _exp, n in runs)):
        app._do_tick()
        yield app


def test_new_hud_capture_shows_no_wrong_percentages(capsys):
    wrong_rows = wrong_totals = backwards = 0
    established_at = None
    previous_gain = None
    for tick, app in enumerate(_replay(NEW_HUD_29MIN)):
        cur, pct = app._last.exp_cur, app._last.exp_pct
        if cur is None:
            continue
        total = app._session.total_exp
        if app._session._exp_est.total is not None and established_at is None:
            established_at = tick
        if established_at is None:
            continue  # before a consensus exists the HUD shows readings as they come
        # 0.02: the game's own two-decimal display, plus the rounding-vs-
        # truncation ambiguity the estimator allows for.
        if pct is None or abs(cur / TOTAL * 100 - pct) > 0.02:
            wrong_rows += 1
        if abs(total / TOTAL - 1) > 0.002:
            wrong_totals += 1
        gain = app._session.exp_diff
        if gain is not None and previous_gain is not None and gain < previous_gain:
            backwards += 1
        previous_gain = gain
    capsys.readouterr()  # 3,408 ticks of debug logging
    assert established_at is not None and established_at < 60  # within ~30s of play
    assert wrong_rows == 0
    assert wrong_totals == 0
    assert backwards == 0
    assert app._session.exp_diff == 100_141 - 50_355
    assert app._session.start_exp == 50_355


def test_a_session_that_starts_on_a_misread_recovers_its_baseline(capsys):
    """Idle at 5255, read as 'S255' -> 255 for the first several minutes, so
    calibration's three-ticks-agree had nothing else to compare with. v1.0.5
    finished this capture at +16,058; the character really gained 11,058
    (16313 - 5255)."""
    for app in _replay(STARTS_ON_A_MISREAD):
        pass
    capsys.readouterr()
    assert app._session.exp_reading[0] == 16_313
    assert 5_255 <= app._session.start_exp <= 5_255 + 600
    assert 11_058 - 600 <= app._session.exp_diff <= 11_058


def test_fused_value_and_percentage_books_no_phantom_gain(capsys):
    """'EXP629075(76 84716261' and 'EXP 930458(7162]': the value fused with
    the percentage, and a '(' stood in for the bracket, so the parser let it
    through with no percentage to check. v1.0.5 finished this capture at
    +823,919 -- nearly twice the level's whole EXP. Really +5,224."""
    worst = 0
    for app in _replay(NO_BRACKET_MERGE):
        worst = max(worst, app._session.exp_diff or 0)
    capsys.readouterr()
    assert app._session.exp_diff == 5_224
    assert worst == 5_224
