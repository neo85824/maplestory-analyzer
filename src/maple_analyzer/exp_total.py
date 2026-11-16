"""Establish the current level's total EXP from many readings, and use it to
tell a good EXP reading from a misread one.

The game shows EXP as `cur[pct%]`. Those are two OCR readings of one quantity:
cur / (pct/100) is the EXP the whole level needs, a constant until level-up.
A single tick's ratio is only as good as that tick's OCR, and the OCR is not
good -- in a 29-minute live capture (2026-09-12, 1366x768, new HUD) about one
EXP reading in six had a dropped or swapped digit:

    'EXP 98700[1 50%]'    pct lost a digit  (really 12.50)
    'EXP 92489[11 21%]'   7 read as 2       (really 11.71)
    'EXP 9991[12 66%]'    cur lost a digit  (really 99991)
    'EXP 901659[11 42%]'  cur gained one    (really 90165)

Every percentage the HUD shows (EXP gained %, projected %, the level-up ETA)
divides by that total, so each of those ticks put a wrong number on screen --
ten times too small in the first case -- and an idle character holds the same
misread for as long as it stands still.

What separates truth from misreads is that correct readings all imply the
*same* total while misreads scatter. Each reading constrains the total to an
interval (pct is only known to 2 decimals); the total is taken as the point
that the most *distinct* readings agree on.

Two properties that matter, both learned the hard way elsewhere in this app
(see rate.py's _LossTracker and Session docstrings):

- Repetition is not corroboration. A covered panel or an idle character
  repeats the identical text every tick, so support is counted over distinct
  (cur, pct) pairs -- a thousand copies of one reading count once.
- A systematic misread must not be able to certify itself. Dropping the
  leading digit of pct ('11.83' -> '1.83') yields totals that agree with each
  other for a few consecutive readings, because neighbouring wrong totals are
  close. They drift apart as pct moves, though, where true ones do not -- so a
  cluster only counts once its members span MIN_PCT_SPAN of percentage, which
  is wider than any such run can stay consistent across (at most 4 steps of
  0.01, for a dropped leading '1' near 19.99%).

Until a total is established this class has no opinion (`observe` returns
None) and callers fall back to whatever they did before.
"""
from __future__ import annotations

from collections import deque


class LevelTotalEstimator:
    # pct is displayed to 2 decimals. Whether the game rounds or truncates is
    # not known for certain (the live data fits rounding better), so a reading
    # of p is taken to mean the true percentage is within a full 0.01 either
    # side -- wide enough for both.
    PCT_ULP = 0.01
    # Distinct readings that must agree before a total is believed.
    MIN_SUPPORT = 3
    # ...and the percentage range they must cover between them. See the module
    # docstring: this is what stops a systematic misread certifying itself.
    MIN_PCT_SPAN = 0.05
    # Distinct readings remembered. Old ones age out, so a total is always
    # backed by recent evidence.
    WINDOW = 80
    # A different total replaces an established one only with this much
    # support, and only once it either outvotes the established one 2:1 in the
    # window or SWITCH_MISS_STREAK distinct readings in a row have disagreed
    # with the established one. This is the way out of a missed level-up (the
    # LV badge unreadable, so nothing announced the new level) and of a total
    # that was established wrongly.
    SWITCH_SUPPORT = 5
    SWITCH_MISS_STREAK = 8

    # Once a total is established, a reading with no percentage cannot be
    # verified and is skipped -- unless the percentage has been missing for
    # this many ticks running, in which case waiting for one is pointless and
    # cur is accepted on its bounds alone (never below the last good reading,
    # never reaching the level total).
    NO_PCT_PATIENCE = 10
    # EXP only rises within a level, so a reading *below* the last good one is
    # suspect even when it agrees with the total -- which a reading that lost a
    # digit from both numbers does: '8721[111%]' for 87210[11.04%] is the same
    # ratio. It is believed only once this many distinct readings, or this
    # many ticks, have said so; that is what recovers from the opposite case,
    # where the reading that was wrong was the earlier, higher one.
    DROP_DISTINCT = 3
    DROP_TICKS = 10

    def __init__(self) -> None:
        self._obs: deque[tuple[int, float, float, float]] = deque(maxlen=self.WINDOW)
        self._region: tuple[float, float] | None = None
        self._miss_streak = 0
        self._level: int | None = None
        self._last_cur: int | None = None   # last trusted reading...
        self._last_pct: float | None = None  # ...and the percentage for it
        self._no_pct_streak = 0
        self._drop_seen: set[tuple[int, float]] = set()
        self._drop_ticks = 0

    def reset(self) -> None:
        self._obs.clear()
        self._region = None
        self._miss_streak = 0
        self._last_cur = None
        self._last_pct = None
        self._no_pct_streak = 0
        self._clear_drop()

    def _clear_drop(self) -> None:
        self._drop_seen = set()
        self._drop_ticks = 0

    # ---- reading the result ---------------------------------------------

    @property
    def total(self) -> float | None:
        """The level's total EXP, or None until one is established."""
        if self._region is None:
            return None
        return (self._region[0] + self._region[1]) / 2

    @property
    def reading(self) -> tuple[int | None, float | None]:
        """The last trusted (cur, pct) -- what a display should show. Before a
        total is established every reading is taken as it comes, so this is
        simply the latest one."""
        return self._last_cur, self._last_pct

    def agrees(self, cur: int, pct: float) -> bool | None:
        """Do cur and pct agree with the established total, to within pct's
        display precision? None while there is no total to compare against.

        A pure question about one pair -- unlike observe(), it knows nothing
        of what came before, so it does not catch a both-numbers-lost-a-digit
        reading (see DROP_DISTINCT)."""
        if self._region is None:
            return None
        lo, hi = self._region
        return (pct - self.PCT_ULP) <= cur * 100 / lo and (pct + self.PCT_ULP) >= cur * 100 / hi

    def pct_of(self, cur: int) -> float | None:
        """cur as a percentage of the established total (None without one)."""
        total = self.total
        if not total:
            return None
        return cur / total * 100

    # ---- feeding it -----------------------------------------------------

    def observe(self, cur: int | None, pct: float | None, level: int | None = None) -> bool | None:
        """Take in one tick's EXP reading -- call exactly once per tick.

        Returns whether the reading is trusted: True/False once a total is
        established, None before that (no opinion; the reading is still
        remembered as the latest). An unreadable tick (cur None) is None too.
        """
        if cur is None:
            return None
        if (
            level is not None and self._level is not None and level > self._level
            and self._last_cur is not None and cur < self._last_cur
        ):
            # Level-up: the total is a different number now. Both signals are
            # required, same as Session._record_exp -- a one-off LV misread
            # alone must not throw the established total away.
            self.reset()

        if pct is not None and pct > 2 * self.PCT_ULP and cur > 0:
            lo = cur * 100 / (pct + self.PCT_ULP)
            hi = cur * 100 / (pct - self.PCT_ULP)
            if not any(o[0] == cur and o[1] == pct for o in self._obs):
                agrees = self._region is None or (lo <= self._region[1] and hi >= self._region[0])
                self._miss_streak = 0 if agrees else self._miss_streak + 1
                self._obs.append((cur, pct, lo, hi))
                self._reestimate()

        verdict = self._judge(cur, pct)
        if verdict is not False:
            self._last_cur = cur
            self._last_pct = pct if pct is not None else self.pct_of(cur)
            if level is not None:
                self._level = level
        return verdict

    def _judge(self, cur: int, pct: float | None) -> bool | None:
        if self._region is None:
            return None
        if pct is None:
            self._no_pct_streak += 1
            if self._no_pct_streak <= self.NO_PCT_PATIENCE:
                return False
            return (self._last_cur is None or cur >= self._last_cur) and cur < self._region[1]
        self._no_pct_streak = 0
        if not self.agrees(cur, pct):
            return False
        if self._last_cur is not None and cur < self._last_cur:
            self._drop_seen.add((cur, pct))
            self._drop_ticks += 1
            if len(self._drop_seen) < self.DROP_DISTINCT and self._drop_ticks < self.DROP_TICKS:
                return False
        self._clear_drop()
        return True

    def _reestimate(self) -> None:
        best = self._best_cluster()
        if best is None:
            return  # nothing new to say; an established total stays put
        lo, hi, support = best
        if self._region is None or (lo <= self._region[1] and hi >= self._region[0]):
            self._region = (lo, hi)
            return
        # A valid cluster that disagrees with the established total.
        incumbent = sum(1 for o in self._obs if o[2] <= self._region[1] and o[3] >= self._region[0])
        if support >= self.SWITCH_SUPPORT and (
            support >= 2 * incumbent or self._miss_streak >= self.SWITCH_MISS_STREAK
        ):
            self._region = (lo, hi)
            self._miss_streak = 0
            self._last_cur = None
            self._clear_drop()

    def _best_cluster(self) -> tuple[float, float, int] | None:
        """The total the most distinct readings agree on, as (lo, hi, support)
        -- or None if no group passes MIN_SUPPORT and MIN_PCT_SPAN.

        The point covered by the most intervals always sits at some interval's
        lower bound, so those are the only candidates worth testing.
        """
        obs = self._obs
        best: tuple[float, float, int] | None = None
        for _c, _p, point, _hi in obs:
            members = [o for o in obs if o[2] <= point <= o[3]]
            if len(members) < self.MIN_SUPPORT or (best is not None and len(members) <= best[2]):
                continue
            pcts = [m[1] for m in members]
            if max(pcts) - min(pcts) < self.MIN_PCT_SPAN - 1e-9:
                continue
            best = (max(m[2] for m in members), min(m[3] for m in members), len(members))
        return best
