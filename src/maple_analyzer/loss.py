"""HP/MP loss accounting -- see LossTracker."""
from __future__ import annotations


class LossTracker:
    """Accumulates the downward side of one stat's per-tick deltas, with two
    guards against OCR noise.

    Why guards are needed here specifically: HP/MP loss is a *running total*
    that only ever increases, so a single bad reading is baked in permanently
    -- unlike EXP, which is end-minus-start and self-corrects on the next
    tick. Observed live: an idle character (zero real MP spend) accumulated
    142,258 MP of "loss", ~50x the character's max MP, purely from misreads.

    Guard 1 -- max stability. `max` is constant except at level-up, so a tick
    reporting a different one was misparsed and is discarded whole. This
    catches the expensive modes, where a misread '/' shifts a digit across the
    divider: '1663/2816' -> '16632/816' reads *high*, costs nothing that tick,
    then books a 14,969 phantom loss when the next correct read "drops" back.
    A genuinely new max (level-up) is accepted once a second tick corroborates
    it, so this can't wedge the tracker permanently -- or, without a level
    bump alongside it, once a third tick does (see _accept_max: a level-up
    IS the corroboration that a max change is real, so it needs one less tick
    than a max change with no level movement at all).

    Guard 2 -- outlier hold. A reading within OUTLIER_FRACTION of max from the
    last accepted value is normal play and is taken immediately (no lag). A
    reading further out than that has to be corroborated by a second reading
    near it before it becomes the new baseline; otherwise it is held aside and
    forgotten the moment a normal reading arrives.

    Two earlier designs failed here and are worth not repeating:

    - Guarding only *drops* leaks badly (48k of phantom loss in a 3-minute
      idle simulation), because a phantom *high* read poisons the baseline
      instantly and the true values then "drop" back to reality and
      corroborate each other perfectly. The guard must be symmetric.
    - A median-of-3 despike is symmetric but only survives *isolated* spikes.
      Under the alternating good/bad pattern real OCR produces
      (1663, 3, 1663, 16, ...) the median window itself is half garbage and
      the filter passes the noise straight through.

    Outlier hold survives both: the alternating case never corroborates, and
    a genuine large change (a one-shot, a full-heal) simply lands one tick
    late. Normal-sized changes aren't delayed at all.

    Cost: a real, large, single-tick dip that fully recovers before the next
    read isn't counted -- but at 2Hz such a dip was already invisible half the
    time. A *persistently* misread value is booked once, then becomes the
    baseline; bounded, unlike the old unbounded ratchet.

    Establishing `max` in the first place is a separate concern, owned by
    Session's calibration (see its docstring) -- this class never adopts a
    max on its own. `confirm_max()` is the only way `_max` is set from
    nothing; before that call, `record()` treats every tick as unusable and
    is a no-op, exactly like a tick with no maximum at all.
    """

    # Fraction of max MP/HP a single 500ms tick may move before the reading
    # is treated as suspect. Generous on purpose: this only decides what needs
    # corroboration, not what counts, so a real big hit still lands (one tick
    # late) while digit-truncation misreads -- which are always wrong by most
    # of the bar -- are the ones held.
    OUTLIER_FRACTION = 0.5

    def __init__(self) -> None:
        self.loss = 0
        self._last: int | None = None       # last accepted value
        self._max: int | None = None        # established max for this stat
        self._max_candidate: int | None = None
        self._max_candidate_count = 0
        self._max_candidate_level_bumped = False
        self._last_level: int | None = None
        self._candidate: int | None = None  # outlier awaiting corroboration

    def confirm_max(self, maximum: int) -> None:
        """Called once by Session's calibration, when `maximum` has been
        corroborated by independent means (see rate.py's _MaxCalibration) -- this is the only place `_max` is ever set from
        nothing. `_last` is deliberately left alone: the very next record()
        call baselines off its `cur` with no loss booked, same as the first
        tick of a stat that's never seen a max at all."""
        self._max = maximum

    def reset(self, last: int | None) -> None:
        """New session: zero the total, keep the established max (it survives
        session boundaries), baseline off the last known value."""
        self.loss = 0
        self._last = last
        self._candidate = None

    def rebaseline(self, cur: int | None) -> None:
        """Re-anchor without booking a loss -- used when resuming from a
        pause (see Session.resume): whatever happened while paused is
        invisible to the session, so the first post-resume reading must not
        be diffed against the pre-pause value."""
        if cur is not None:
            self._last = cur
        self._candidate = None

    def record(self, cur: int | None, maximum: int | None = None, level: int | None = None) -> None:
        if cur is None:
            return
        if maximum is not None:
            implausible = self._max is not None and self._max_is_implausible(maximum)
            accepted = self._accept_max(maximum, level)
            if level is not None:
                self._last_level = level
            if not accepted and not implausible:
                return  # a legitimate max-change candidate, still awaiting corroboration
            if not accepted:
                # An implausible max means *that digit* was misparsed -- it
                # doesn't mean cur was. A max whose digits straddle a fragile
                # spot (e.g. a trailing digit that blends into the closing
                # bracket) can misread on the majority of ticks, and bailing
                # out on the whole tick every time would throw away nearly
                # all of a session's real cur readings along with the bad max
                # ones (see test_rejected_max_falls_back_to_established_max_for_cur_check).
                # But cur is only trustworthy here if it's *also* a small,
                # sane move from the last accepted value -- a genuinely
                # covered/garbage frame typically corrupts cur just as badly
                # as max, and a wild cur must still be rejected outright
                # rather than risk it quietly corroborating a second wild
                # reading later (see test_no_single_tick_books_a_large_phantom_loss).
                if self._last is None:
                    return
                tolerance = self._max * self.OUTLIER_FRACTION
                if abs(cur - self._last) > tolerance:
                    return
            elif cur > maximum:
                return  # cur can never exceed max; this reading is garbage
        if self._last is None:
            self._last = cur
            return
        tolerance = (self._max or self._last) * self.OUTLIER_FRACTION
        if abs(cur - self._last) <= tolerance:
            self._commit(cur)
        elif self._candidate is not None and abs(cur - self._candidate) <= tolerance:
            self._commit(cur)  # corroborated -- a real jump after all
        else:
            self._candidate = cur  # hold; a normal reading next tick discards it

    def _commit(self, cur: int) -> None:
        if cur < self._last:
            self.loss += self._last - cur
        self._last = cur
        self._candidate = None

    # A level-up nudges max HP/MP; nothing in the game multiplies it. A
    # proposed max outside this factor of the established one is garbage and
    # is never adopted, no matter how many ticks repeat it -- corroboration
    # alone is not enough, because when a window covers the panel the OCR
    # garbage is *static*: the identical wrong text every tick. That is how a
    # max of 281616 (from '281616' misread out of '2816') got installed in a
    # live capture, after which a bogus cur of 28163 passed every check and
    # booked 25,347 of phantom loss when the panel came back.
    MAX_CHANGE_FACTOR = 2.0

    def _max_is_implausible(self, maximum: int) -> bool:
        """True if `maximum` is too far from the established max to ever be a
        real level-up change -- see MAX_CHANGE_FACTOR's docstring above.
        Assumes self._max is not None; callers check that first."""
        return not (self._max / self.MAX_CHANGE_FACTOR <= maximum <= self._max * self.MAX_CHANGE_FACTOR)

    def _accept_max(self, maximum: int, level: int | None) -> bool:
        if maximum <= 0:
            return False
        if self._max is None:
            # Session's calibration (require_calibration=True) always calls
            # confirm_max() before this tracker ever sees a real record()
            # call, so this branch only fires with calibration off -- same
            # unconditional first-adopt as before that feature existed.
            self._max = maximum
            return True
        if maximum == self._max:
            self._max_candidate = None
            self._max_candidate_count = 0
            return True
        if self._max_is_implausible(maximum):
            return False  # implausible -- never adopt, however often it repeats
        level_bumped = (
            level is not None and self._last_level is not None and level > self._last_level
        )
        if maximum != self._max_candidate:
            self._max_candidate = maximum
            self._max_candidate_count = 1
            # Captured at the moment the candidate first appears, not
            # re-evaluated every tick after -- a level-up IS the
            # corroboration, so it only counts for the change it accompanies,
            # not for an unrelated max blip that happens to follow one.
            self._max_candidate_level_bumped = level_bumped
        else:
            self._max_candidate_count += 1
        required = 2 if self._max_candidate_level_bumped else 3
        if self._max_candidate_count >= required:
            self._max = maximum
            self._max_candidate = None
            self._max_candidate_count = 0
            return True
        return False
