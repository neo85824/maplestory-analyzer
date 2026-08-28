"""OverlayApp's run-state machine: running/paused/stopped, auto-stop,
restart-with-save, deleting a History entry, and the timer/auto-finalize
staying alive while OCR capture is failing.

Same technique as test_tick_loop.py: poke OverlayApp's unbound methods
against a stub `self` rather than constructing the real app (which needs a
live Tk display) -- what's under test here is control flow and Session
wiring, not widgets. _StubWidget stands in for every CTk widget OverlayApp
touches (.configure/.cget/.grid/.grid_remove/.grid_info/.set), which is
enough for every method exercised below; none of them inspect real Tk
geometry or fonts beyond what _t()/_font() compute from Settings.language.
"""
from __future__ import annotations

from collections import defaultdict

import pytest

from maple_analyzer import overlay as overlay_module
from maple_analyzer.overlay import OverlayApp
from maple_analyzer.parser import StatSnapshot
from maple_analyzer.rate import Session, SessionSummary
from maple_analyzer.settings import Settings


class _StubWidget:
    """Minimal stand-in for any CTkLabel/CTkButton/CTkFrame OverlayApp calls
    .configure/.cget/.grid/.grid_remove/.grid_info/.set on."""

    def __init__(self):
        self._opts: dict = {}
        self._gridded = True

    def configure(self, **kw):
        self._opts.update(kw)

    def cget(self, key):
        return self._opts.get(key)

    def grid(self, **kw):
        self._gridded = True

    def grid_remove(self):
        self._gridded = False

    def grid_info(self):
        return {"in": "parent"} if self._gridded else {}

    def set(self, value):  # CTkProgressBar
        self._opts["value"] = value


class _StubRoot:
    def attributes(self, *_a, **_kw):
        pass


class _StubSource:
    """Fakes capture.py's PanelSource: grab_fields() returns already-"OCR'd"
    text directly (see _StubApp.__init__, which points _ocr.read_field at an
    identity function), so no real screen capture or OCR model is needed."""

    TOTAL = 500_000.0

    def __init__(self):
        self.hp, self.mp, self.exp, self.level = 800, 2800, 100, 44
        self.blocked = False

    def _pct(self) -> float:
        return self.exp / self.TOTAL * 100

    def grab_fields(self):
        if self.blocked:
            raise RuntimeError("stat panel is obscured")
        return {
            "LV": f"LV. {self.level}",
            "HP": f"HP[{self.hp}/824]",
            "MP": f"MP[{self.mp}/2816]",
            "EXP": f"EXP {self.exp}[{self._pct():.2f}%]",
        }


class _StubOcr:
    @staticmethod
    def read_field(img):
        return img  # _StubSource already hands back text, not an image


class _StubApp:
    """Assembles just enough of OverlayApp's instance state for the unbound
    methods under test to run. run_state defaults to "stopped", matching
    OverlayApp.__init__ (see overlay.py's _run_state docstring)."""

    def __init__(self):
        self._settings = Settings()
        self._session = Session()
        self._session_history: list[SessionSummary] = []
        self._history_cards: list = []
        self._last = StatSnapshot(None, None, None, None, None, None, None)
        self._source = _StubSource()
        self._ocr = _StubOcr()
        self._modal_open = False
        self._run_state = "stopped"
        self._last_capture_error: str | None = None
        self._last_client_size = None
        self.root = _StubRoot()

        self._status_pill = _StubWidget()
        self._timer_label = _StubWidget()
        self._pause_button = _StubWidget()
        self._restart_button = _StubWidget()
        self._stop_button = _StubWidget()
        self._value_labels: dict = defaultdict(_StubWidget)
        self._bars: dict = defaultdict(_StubWidget)

        self.rebuild_calls = 0

    def _append_history_card(self, _summary, _index):
        pass  # card rendering is out of scope -- only session_history matters here

    def _rebuild_history_cards(self):
        self.rebuild_calls += 1

    # Bound to the real implementations -- these are the actual behaviour
    # under test, not stand-ins.
    _t = OverlayApp._t
    _font = OverlayApp._font
    _localize_error = OverlayApp._localize_error
    _set_status_error = OverlayApp._set_status_error
    _log = staticmethod(OverlayApp._log)
    _modal = OverlayApp._modal
    _do_tick = OverlayApp._do_tick
    _render = OverlayApp._render
    _update_timer_label = OverlayApp._update_timer_label
    _maybe_finalize_on_timeout = OverlayApp._maybe_finalize_on_timeout
    _commit_session_to_history = OverlayApp._commit_session_to_history
    _finalize_and_maybe_stop = OverlayApp._finalize_and_maybe_stop
    _on_restart_clicked = OverlayApp._on_restart_clicked
    _on_stop_clicked = OverlayApp._on_stop_clicked
    _on_pause_button_clicked = OverlayApp._on_pause_button_clicked
    _apply_run_state = OverlayApp._apply_run_state
    _on_delete_history_clicked = OverlayApp._on_delete_history_clicked


def _calibrate(app: _StubApp, gains=(100,)) -> None:
    """Run enough ticks to clear Session's calibration gate -- see
    test_baseline_confirmation.py for what this is actually guarding."""
    app._do_tick()
    for gain in gains:
        app._source.exp += gain
        app._do_tick()


# --- launching stopped, not tracking ------------------------------------


def test_app_launches_stopped_and_does_not_track():
    app = _StubApp()
    for _ in range(5):
        app._do_tick()
    assert app._run_state == "stopped"
    assert app._session.is_calibrating  # never fed a tick
    assert app._session.start_exp is None
    assert app._session_history == []


def test_stopped_app_still_shows_live_ocr_readouts():
    """Not tracking a session is not the same as a frozen HUD -- LV/HP/MP/EXP
    keep reading from the game while stopped, same as while paused."""
    app = _StubApp()
    app._do_tick()
    assert app._value_labels["level"].cget("text") == "44"
    assert app._value_labels["hp"].cget("text") == "800/824"


def test_start_button_begins_tracking():
    app = _StubApp()
    app._on_pause_button_clicked()  # "stopped" role: Start
    assert app._run_state == "running"
    _calibrate(app, gains=(100,))
    assert not app._session.is_calibrating
    assert app._session.start_exp == 100


def test_start_after_stop_baselines_off_the_latest_exp_not_the_stale_one():
    """record() -- and with it Session's cached exp_cur -- is a no-op the
    whole time the app is "stopped" (see Session.pause()), but _do_tick keeps
    updating app._last from live OCR regardless (that's how the HUD keeps
    showing current numbers while stopped). EXP earned during that window
    must not be silently absorbed into the next session's baseline."""
    app = _StubApp()
    app._on_pause_button_clicked()  # stopped -> running
    _calibrate(app, gains=(100,))
    app._settings.window_min = 1
    app._session._start_time -= 61  # force auto-stop
    app._do_tick()
    assert app._run_state == "stopped"
    app._source.exp += 500  # game keeps earning EXP while the overlay sits stopped
    app._do_tick()  # HUD updates (app._last), but the stopped session does not record it
    app._on_pause_button_clicked()  # "stopped" role: Start
    assert app._session.start_exp == app._last.exp_cur


# --- pause/resume via the same button -------------------------------------


def test_pause_button_cycles_running_paused_running():
    app = _StubApp()
    app._on_pause_button_clicked()  # stopped -> running
    _calibrate(app, gains=(100,))
    app._on_pause_button_clicked()  # running -> paused
    assert app._run_state == "paused"
    assert app._pause_button.cget("text") == app._t("resume_button")
    app._on_pause_button_clicked()  # paused -> running
    assert app._run_state == "running"
    assert app._pause_button.cget("text") == app._t("pause_button")


# --- auto-stop (default on) -----------------------------------------------


def test_auto_stop_commits_and_stops_by_default():
    app = _StubApp()
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._settings.window_min = 1
    app._session._start_time -= 61  # force the interval to have elapsed
    app._do_tick()
    assert app._run_state == "stopped"
    assert len(app._session_history) == 1
    assert app._restart_button.grid_info() == {}  # hidden while stopped
    elapsed_at_stop = app._session.elapsed()
    app._do_tick()
    assert app._session.elapsed() == elapsed_at_stop  # frozen, not overrunning


def test_auto_stop_disabled_restarts_instead():
    app = _StubApp()
    app._settings.auto_stop = False
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._settings.window_min = 1
    app._session._start_time -= 61
    app._do_tick()
    assert app._run_state == "running"  # old behaviour: immediately restarts
    assert len(app._session_history) == 1
    assert app._session.start_exp is not None  # carried forward, not calibrating again


# --- restart-with-save (default on) ---------------------------------------


def test_restart_saves_to_history_by_default():
    app = _StubApp()
    assert app._settings.save_on_restart is True
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._session._start_time -= 5  # past the 1s noise guard in _commit_session_to_history
    app._on_restart_clicked()
    assert len(app._session_history) == 1
    assert app._run_state == "running"


def test_restart_can_be_configured_to_discard():
    app = _StubApp()
    app._settings.save_on_restart = False
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._session._start_time -= 5
    app._on_restart_clicked()
    assert app._session_history == []


# --- manual Stop button -----------------------------------------------


def test_stop_button_commits_and_stops():
    app = _StubApp()
    app._on_pause_button_clicked()  # stopped -> running
    _calibrate(app, gains=(100,))
    app._session._start_time -= 5  # past the 1s noise guard in _commit_session_to_history
    app._on_stop_clicked()
    assert app._run_state == "stopped"
    assert len(app._session_history) == 1
    assert app._restart_button.grid_info() == {}  # hidden while stopped
    assert app._stop_button.grid_info() == {}  # hidden while stopped
    elapsed_at_stop = app._session.elapsed()
    app._do_tick()
    assert app._session.elapsed() == elapsed_at_stop  # frozen, not overrunning


def test_stop_button_always_commits_regardless_of_save_on_restart():
    """Unlike Restart, Stop has no "next session" to weigh discarding against
    -- save_on_restart governs that tradeoff for Restart only."""
    app = _StubApp()
    app._settings.save_on_restart = False
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._session._start_time -= 5
    app._on_stop_clicked()
    assert len(app._session_history) == 1


def test_stop_button_reachable_from_paused():
    app = _StubApp()
    app._on_pause_button_clicked()  # stopped -> running
    _calibrate(app, gains=(100,))
    app._session._start_time -= 5
    app._on_pause_button_clicked()  # running -> paused
    app._on_stop_clicked()
    assert app._run_state == "stopped"
    assert len(app._session_history) == 1


def test_stop_button_hidden_while_already_stopped():
    app = _StubApp()
    app._apply_run_state()  # OverlayApp.__init__ calls this once to lay out the "stopped" launch state
    assert app._stop_button.grid_info() == {}
    assert app._restart_button.grid_info() == {}


def test_stop_button_visible_while_running_or_paused():
    app = _StubApp()
    app._on_pause_button_clicked()  # stopped -> running
    assert app._stop_button.grid_info() != {}
    app._on_pause_button_clicked()  # running -> paused
    assert app._stop_button.grid_info() != {}


# --- timer/auto-finalize survive a blocked capture window -----------------


def test_timer_label_keeps_moving_while_capture_is_blocked():
    app = _StubApp()
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._session._start_time -= 5
    app._source.blocked = True
    app._do_tick()
    assert app._timer_label.cget("text") == app._t("timer_left", time="9:55")


def test_auto_finalize_still_fires_while_capture_is_blocked():
    """The bug being guarded: elapsed() is wall-clock, not tick-driven, so a
    session must still hit its interval and auto-stop even if the game
    window stays covered for the whole window, not just while OCR succeeds."""
    app = _StubApp()
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._settings.window_min = 1
    app._session._start_time -= 61
    app._source.blocked = True
    app._do_tick()
    assert app._run_state == "stopped"
    assert len(app._session_history) == 1


def test_stopped_session_does_not_auto_finalize_again():
    app = _StubApp()
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._settings.window_min = 1
    app._session._start_time -= 61
    app._do_tick()
    assert app._run_state == "stopped"
    history_len = len(app._session_history)
    for _ in range(5):
        app._do_tick()
    assert len(app._session_history) == history_len  # no repeated commits


# --- projected session EXP -------------------------------------------------


def test_projected_exp_is_rendered_once_there_is_a_positive_rate():
    app = _StubApp()
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._session._start_time -= 10  # clear the 3s-elapsed guard
    app._source.exp += 50
    app._do_tick()
    assert app._value_labels["projexp"].cget("text") != "--"


def test_projected_exp_shows_placeholder_before_enough_signal():
    app = _StubApp()
    app._on_pause_button_clicked()
    _calibrate(app, gains=(100,))
    app._do_tick()  # no time has passed yet
    assert app._value_labels["projexp"].cget("text") == "--"


# --- deleting one History entry --------------------------------------------


def _fake_summary(name: str | None = None) -> SessionSummary:
    return SessionSummary(
        start_time=0.0, end_time=60.0, start_exp=100, end_exp=200,
        hp_loss=0, mp_loss=0, total_exp=None, interval_minutes=10, name=name,
    )


def test_delete_history_removes_the_confirmed_entry(monkeypatch):
    app = _StubApp()
    app._session_history[:] = [_fake_summary("keep"), _fake_summary("delete me")]
    monkeypatch.setattr(overlay_module.messagebox, "askyesno", lambda *a, **kw: True)
    app._on_delete_history_clicked(2)  # 1-based -- "delete me" is index 2
    assert [s.name for s in app._session_history] == ["keep"]
    assert app.rebuild_calls == 1


def test_delete_history_cancelled_keeps_the_entry(monkeypatch):
    app = _StubApp()
    app._session_history[:] = [_fake_summary("a"), _fake_summary("b")]
    monkeypatch.setattr(overlay_module.messagebox, "askyesno", lambda *a, **kw: False)
    app._on_delete_history_clicked(1)
    assert [s.name for s in app._session_history] == ["a", "b"]
    assert app.rebuild_calls == 0


def test_delete_history_prompt_names_the_session(monkeypatch):
    app = _StubApp()
    app._session_history[:] = [_fake_summary("grinding spot A")]
    seen = {}

    def _fake_askyesno(title, prompt, **kw):
        seen["title"], seen["prompt"] = title, prompt
        return False

    monkeypatch.setattr(overlay_module.messagebox, "askyesno", _fake_askyesno)
    app._on_delete_history_clicked(1)
    assert "grinding spot A" in seen["prompt"]
