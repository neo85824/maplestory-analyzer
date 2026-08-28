"""Always-on-top HUD: capture -> OCR -> parse -> session -> redraw, on a timer.

Per-tick timing (2026-08-17 rework, measured against the live game): the
original whole-panel detection+recognition OCR pass was ~600-680ms/tick --
detection (finding text regions) was the entire cost, not recognition. Since
regions.py's FIELD_BOXES already pins down exactly where each field's text
is, detection was pure waste; switched to four small recognition-only calls
(no detection stage) on individually pre-cropped fields, ~15ms each, ~60ms
total. Capture itself is ~3.5ms. `_tick()` now also computes its own elapsed
work time and schedules the next call at `TARGET_MS - elapsed`, floored at
0, instead of the old fixed post-delay (which added TARGET_MS on top of
whatever the work took, so it could never reach the target rate no matter
how fast OCR got) -- this is what actually makes the real cycle approach
TARGET_MS rather than merely bound the *added* delay to it.

UI (2026-08-17 rework, restyled same day per an HTML design preview the user
approved): CustomTkinter, three tabs (Live/History/Settings). Status/session
timer live in their own strip at the top of Live (a pill + a chip, not just
another text row); stats and session info sit in aligned grids with tabular
numerals; History renders each session as a card, not scrollback text. See
~/.claude/notes/maplestory-analyzer/final-spec-2026-08-17.md Section 3 for
the full spec. This module still only calls Session's public methods and
reads StatSnapshot/SessionSummary fields -- the capture/OCR/parser engine
(capture.py/ocr.py/parser.py/regions.py) is untouched by this rework, per
the hard UI/engine separation rule in that same doc.

Settings + i18n (2026-08-17, later same day): all UI-layer settings live in
one `Settings` struct (settings.py) instead of scattered instance attributes,
so a future persistence layer can load/save it wholesale. All user-facing
strings route through `self._t(key)` into i18n.py's translation table (English
+ Traditional Chinese, zh default) instead of literals inline here -- static
widgets built once register themselves in `self._i18n_labels` so a language
switch can walk the list and reconfigure every one of them, tabs get renamed
via CTkTabview.rename(), and History cards (built dynamically per session) are
simply torn down and rebuilt from `self._session_history` on switch.
"""
from __future__ import annotations

import contextlib
import dataclasses
import os
import sys
import time
import tkinter as tk
from tkinter import messagebox, simpledialog
from typing import Protocol

import customtkinter as ctk

from .capture import PANEL_OBSCURED
from .i18n import Lang, t
from .ocr import StatPanelOcr
from .parser import StatSnapshot, parse_fields
from .rate import Session, SessionSummary
from .settings import Settings

# The console's codepage (e.g. cp950 Traditional Chinese) can't represent
# every character OCR might misread out of the game's UI -- printing one
# used to raise UnicodeEncodeError and silently kill the tick loop (see
# _tick's try/except below for the other half of this fix). errors="replace"
# swaps unencodable characters for '?' instead of crashing.
if sys.stdout is None:
    # PyInstaller's windowed build (console=False) has no stdout/stderr at
    # all (both are None), which crashes not just .reconfigure() below but
    # every bare print() elsewhere in this module (tick-error/debug
    # logging) the moment they run. Swap in a no-op sink so those stay
    # harmless instead of taking down the app.
    #
    # encoding/errors are NOT optional here: open() defaults to the locale
    # codepage with errors='strict', i.e. cp950 on this zh-TW machine. The
    # PP-OCR recognition dictionary is largely *Simplified* Chinese, so a
    # garbage read (game window obscured, floating damage numbers over the
    # panel) routinely produces characters Big5/cp950 cannot encode -- and
    # printing one raised UnicodeEncodeError straight through the sink,
    # killing the tick loop. Same errors="replace" the console path below
    # has always had; the windowed build was the only place missing it.
    sys.stdout = sys.stderr = open(
        os.devnull, "w", encoding="utf-8", errors="replace"
    )
else:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr is not None:
        # Tk writes uncaught-callback tracebacks here, and a traceback can
        # carry the same unencodable OCR text in its repr.
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

if sys.platform == "win32":
    # Without declaring DPI awareness, Windows scales the whole rendered
    # window as a bitmap after the fact -- Tk still thinks the window is
    # e.g. 420 logical px, but the OS-scaled result doesn't match, and
    # widget content ends up clipped past the visible window edge (observed
    # live: value labels cut off mid-digit). Declaring per-monitor-v2
    # awareness lets Windows and Tk agree on actual pixel dimensions instead.
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        pass

TARGET_MS = 500  # target full tick cycle -- 2Hz, per user request
SCALE_STEP_PCT = 10
SCALE_MIN_PCT = 50
SCALE_MAX_PCT = 150

# Live tab's Pause/Resume/Start + Restart button row -- see _apply_run_state.
BUTTON_HEIGHT = 28
STOPPED_BUTTON_WIDTH = 96  # Start alone, centered -- smaller than the two-button width

# Color tokens, matching the approved HTML design preview.
BG = "#0d1117"
SURFACE = "#161b22"
SURFACE_2 = "#1c2230"
INK = "#e6edf3"
INK_DIM = "#8b96a5"
INK_FAINT = "#5b6577"
ACCENT = "#5eead4"
ACCENT_INK = "#06322c"
HP_COLOR = "#ff6b6b"
MP_COLOR = "#5b9dff"
EXP_COLOR = "#ffc247"
OK_COLOR = "#3ddc84"
TRACK_BG = "#12291f"

# Chrome text (tabs, headers, buttons, switches, kv labels -- anything that
# can carry translated content) picks its font family from the active
# language via OverlayApp._font(); Segoe UI has no real Traditional Chinese
# glyphs of its own (falls back to a system CJK font Windows picks for you,
# inconsistent with the rest of the UI), so zh uses Microsoft JhengHei
# (Windows' standard Traditional Chinese UI font) instead.
_FONT_FAMILY: dict[Lang, str] = {"en": "Segoe UI", "zh": "Microsoft JhengHei"}

# Fixed English-only chrome that never carries translated text (the game's
# own on-screen abbreviations LV/HP/MP/EXP, and the +/- scale stepper) stays
# on a plain Segoe UI tuple -- no language switching needed for pure ASCII.
_FONT_LABEL = ("Segoe UI", 10, "bold")
_FONT_UI_BOLD = ("Segoe UI", 13, "bold")

# Pure-numeric value labels (HP/MP/EXP readouts, session EXP diffs, history
# card numbers) stay on Consolas regardless of language -- they never render
# CJK text, and Consolas' monospacing is what keeps tabular digits aligned.
_FONT_MONO = ("Consolas", 12)
_FONT_MONO_SM = ("Consolas", 10)
_FONT_MONO_BOLD = ("Consolas", 12, "bold")


class PanelSource(Protocol):
    def grab_fields(self) -> dict:
        ...


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def _fmt_loss(loss: int) -> str:
    return f"-{loss}" if loss > 0 else "0"


def _fmt_summary(s: SessionSummary, index: int) -> str:
    # Console/debug log only, not shown in the UI -- deliberately left in
    # plain English regardless of self._settings.language.
    diff = s.exp_diff
    diff_s = f"+{diff:,}" if diff is not None else "?"
    pct_diff = s.exp_pct_diff
    pct_s = f" (+{pct_diff:.2f}%)" if pct_diff is not None else ""
    start_s = f"{s.start_exp:,}" if s.start_exp is not None else "?"
    end_s = f"{s.end_exp:,}" if s.end_exp is not None else "?"
    dur_min = s.duration_s / 60
    if s.interval_minutes is not None and abs(dur_min - s.interval_minutes) > 0.05:
        dur_s = f"{dur_min:.1f}m of {s.interval_minutes:.0f}m, restarted early"
    else:
        dur_s = f"{dur_min:.1f}m"
    return (
        f"#{index} ({dur_s}): "
        f"EXP {start_s} -> {end_s} ({diff_s}{pct_s})  "
        f"HP {_fmt_loss(s.hp_loss)}  MP {_fmt_loss(s.mp_loss)}"
    )


class OverlayApp:
    def __init__(self, source: PanelSource):
        self._source = source
        self._ocr = StatPanelOcr()
        self._session = Session()
        self._session_history: list[SessionSummary] = []
        self._settings = Settings()

        self._last: StatSnapshot = StatSnapshot(None, None, None, None, None, None, None)
        # Newest-first: History cards are inserted at index 0 rather than
        # appended, so this tracks the card widgets in display order (index 0
        # = topmost/newest) to pack each new one with before=.
        self._history_cards: list[ctk.CTkFrame] = []
        # Static widgets whose text is a plain translated string (no
        # per-tick data baked in) register themselves here as they're built,
        # so _apply_language() can walk this list and reconfigure every one
        # instead of _build_*_tab needing to be re-run from scratch.
        self._i18n_labels: list[tuple[ctk.CTkBaseClass, str, int, bool]] = []
        # Guards _do_tick's finalize-on-timeout check against the rename
        # dialog's nested event loop -- see _do_tick and _on_rename_clicked.
        self._modal_open = False
        # "running" / "paused" / "stopped" -- see _on_pause_button_clicked and
        # _finalize_and_maybe_stop. "stopped" reached via the timer is
        # implemented by pausing the already-running Session (its clock
        # freezes and record() no-ops, exactly what "stopped" needs) rather
        # than adding a third Session state; starting "stopped" here needs no
        # such call since nothing has fed this fresh Session a tick yet --
        # _do_tick simply doesn't call session.record() until Start is
        # clicked, so it can't begin calibrating or accumulating unasked.
        #
        # Starts stopped rather than tracking immediately on launch -- opening
        # the app (or the .exe) shouldn't silently start a session before the
        # user has actually arrived at the game and decided to track.
        self._run_state = "stopped"
        # Last capture failure message, so _do_tick can log state changes
        # instead of repeating the same line every 2s retry.
        self._last_capture_error: str | None = None
        self._last_client_size: tuple[int, int] | None = None

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        # Applied before the window/widgets are built so the default window
        # size below is already at the configured scale, not built at 100%
        # then rescaled after the fact.
        ctk.set_widget_scaling(self._settings.scale_pct / 100)
        ctk.set_window_scaling(self._settings.scale_pct / 100)

        self.root = ctk.CTk()
        self.root.title("MapleStoryAnalyzer")
        self.root.attributes("-topmost", self._settings.topmost)
        self.root.configure(fg_color=BG)
        self.root.geometry("260x420+40+40")

        self._tabview = ctk.CTkTabview(self.root, fg_color=BG, segmented_button_fg_color=SURFACE)
        self._tabview.pack(fill="both", expand=True, padx=8, pady=8)
        # CTkTabview's tab name doubles as its segmented-button label and its
        # internal dict key -- there's no separate "id" to address a tab by,
        # so the translated string itself is the key. rename() (used by
        # _apply_language) swaps the key/label together and keeps the
        # frame/selection intact; this dict just tracks the current name per
        # logical tab so rename() always has both the old and new string.
        self._tab_names = {
            "live": t("tab_live", self._settings.language),
            "history": t("tab_history", self._settings.language),
            "settings": t("tab_settings", self._settings.language),
        }
        for name in self._tab_names.values():
            self._tabview.add(name)

        self._build_live_tab(self._tabview.tab(self._tab_names["live"]))
        self._build_history_tab(self._tabview.tab(self._tab_names["history"]))
        self._build_settings_tab(self._tabview.tab(self._tab_names["settings"]))
        self._tabview.set(self._tab_names["live"])  # CTkTabview defaults to the last-added tab otherwise
        self._apply_visibility()
        self._apply_run_state()

        self._tick()

    # ---- i18n ------------------------------------------------------------

    def _t(self, key: str, **kwargs: object) -> str:
        return t(key, self._settings.language, **kwargs)

    def _localize_error(self, message: str) -> str:
        """Translate the known capture.py RuntimeError messages (game
        minimized / not found / stat panel covered) shown via _set_status_error --
        these are routine, expected states, not exceptional ones, so they
        deserve a real translation rather than leaking capture.py's raw
        English text into a zh-language UI. Anything unrecognized (a real
        bug, not a known game-window state) passes through unchanged."""
        if message == "game window is minimized":
            return self._t("status_error_minimized")
        if message.startswith("No window found with title containing"):
            return self._t("status_error_not_found")
        if message == PANEL_OBSCURED:
            return self._t("status_error_obscured")
        return message

    def _font(self, size: int, bold: bool = False) -> tuple:
        """Chrome-text font at the given size, in the active language's font
        family (see _FONT_FAMILY). Use for any widget that renders translated
        text; pure-numeric value labels should use the module-level
        _FONT_MONO* constants instead (see their docstring)."""
        family = _FONT_FAMILY[self._settings.language]
        return (family, size, "bold") if bold else (family, size)

    def _scale_header_text(self) -> str:
        return self._t("settings_window_scale") + f" — {self._settings.scale_pct}%"

    def _interval_header_text(self) -> str:
        return self._t("settings_session_interval") + f" — {self._settings.window_min} {self._t('unit_min')}"

    def _i18n(self, widget: ctk.CTkBaseClass, key: str, size: int, bold: bool = True) -> ctk.CTkBaseClass:
        """Set a widget's text + font from a translation key and register it
        for re-translation on language switch. Use for any widget whose text
        is *only* the translated string (no per-tick value baked in) --
        widgets that mix in live data (timer, status pill, kv values) instead
        call self._t(...)/self._font(...) directly wherever they're
        re-rendered every tick."""
        widget.configure(text=self._t(key), font=self._font(size, bold))
        self._i18n_labels.append((widget, key, size, bold))
        return widget

    def _apply_language(self, lang: Lang) -> None:
        if lang == self._settings.language:
            return
        self._settings.language = lang

        for logical, key in (("live", "tab_live"), ("history", "tab_history"), ("settings", "tab_settings")):
            old_name = self._tab_names[logical]
            new_name = self._t(key)
            if new_name != old_name:
                self._tabview.rename(old_name, new_name)
                self._tab_names[logical] = new_name

        for widget, key, size, bold in self._i18n_labels:
            widget.configure(text=self._t(key), font=self._font(size, bold))

        self._status_pill.configure(font=self._font(9, bold=True))
        self._timer_label.configure(font=self._font(10, bold=True))
        self._scale_header_label.configure(text=self._scale_header_text(), font=self._font(11, bold=True))
        self._interval_header_label.configure(text=self._interval_header_text(), font=self._font(11, bold=True))
        # _pause_button's text depends on _run_state, not just language, so it
        # isn't in _i18n_labels -- _apply_run_state() re-derives it from
        # scratch, which also happens to pick up the new language/font.
        self._apply_run_state()

        # History cards mix translated chrome (SESSION #N, HP/MP LOSS) with
        # per-session data and aren't worth tracking widget-by-widget --
        # tearing down and rebuilding from the data we already keep is
        # simpler and this only happens on an explicit language switch, and
        # _append_history_card already picks up the new language/font.
        self._rebuild_history_cards()

        self._render(self._last)  # refreshes status pill / timer text immediately

    # ---- tab construction ------------------------------------------------

    def _build_live_tab(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=1)

        # Status + session timer share one row, both shrunk down (smaller
        # font/padding than the rest of the chrome) so a longer localized
        # status string (the capture-error states from _localize_error run
        # much longer than "Tracking"/"追蹤中") still leaves room for the
        # timer instead of pushing the Restart button out of the window.
        # wraplength caps the status pill's own width so it wraps to a
        # second line rather than growing sideways into the timer's column.
        strip = ctk.CTkFrame(parent, fg_color="transparent")
        strip.grid(row=0, column=0, sticky="ew", padx=2, pady=(2, 3))
        strip.grid_columnconfigure(0, weight=1)
        strip.grid_columnconfigure(1, weight=0)

        self._status_pill = ctk.CTkLabel(
            strip, text=self._t("status_tracking"), corner_radius=999, fg_color=TRACK_BG,
            text_color=OK_COLOR, font=self._font(9, bold=True), padx=8, pady=2,
            anchor="w", justify="left", wraplength=110,
        )
        self._status_pill.grid(row=0, column=0, sticky="w")

        # Mixes translated chrome ("left"/"剩餘") with the countdown digits,
        # so it needs the language-aware font (self._font), not the fixed
        # digits-only _FONT_MONO_BOLD -- unlike the pure-numeric value labels.
        self._timer_label = ctk.CTkLabel(
            strip, text="--:--", corner_radius=999, fg_color=SURFACE_2,
            text_color=INK, font=self._font(10, bold=True), padx=8, pady=2,
        )
        self._timer_label.grid(row=0, column=1, sticky="e")

        # Stat grid: label | mini bar | tabular value, aligned via one grid
        # rather than independently left-justified label:value text. Labels
        # (LV/HP/MP/EXP) are the game's own on-screen abbreviations -- see
        # i18n.py's docstring for why these are not translated.
        stats = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=12)
        stats.grid(row=1, column=0, sticky="ew", padx=2, pady=(0, 3))
        stats.grid_columnconfigure(0, weight=0)
        stats.grid_columnconfigure(1, weight=1)
        stats.grid_columnconfigure(2, weight=0)

        self._stat_rows: dict[str, tuple] = {}
        self._value_labels: dict[str, ctk.CTkLabel] = {}
        self._bars: dict[str, ctk.CTkProgressBar] = {}

        def add_stat_row(row: int, key: str, label_text: str, color: str, with_bar: bool) -> None:
            lbl = ctk.CTkLabel(stats, text=label_text, font=_FONT_LABEL, text_color=color, anchor="w")
            lbl.grid(row=row, column=0, sticky="w", padx=(12, 6), pady=0)
            value = ctk.CTkLabel(stats, text="--", font=_FONT_MONO, text_color=INK, anchor="e")
            value.grid(row=row, column=2, sticky="e", padx=(6, 12), pady=0)
            bar = None
            if with_bar:
                bar = ctk.CTkProgressBar(stats, height=5, progress_color=color, fg_color=SURFACE_2)
                bar.set(0)
                bar.grid(row=row, column=1, sticky="ew", padx=6, pady=0)
                self._bars[key] = bar
            self._stat_rows[key] = (lbl, bar, value)
            self._value_labels[key] = value

        add_stat_row(0, "level", "LV", EXP_COLOR, with_bar=False)
        add_stat_row(1, "hp", "HP", HP_COLOR, with_bar=True)
        add_stat_row(2, "mp", "MP", MP_COLOR, with_bar=True)
        add_stat_row(3, "exp", "EXP", EXP_COLOR, with_bar=True)

        # Session info: label | tabular value, same alignment discipline.
        session_card = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=12)
        session_card.grid(row=2, column=0, sticky="ew", padx=2, pady=(0, 3))
        session_card.grid_columnconfigure(0, weight=1)
        session_card.grid_columnconfigure(1, weight=0)

        self._kv_rows: dict[str, tuple] = {}

        def add_kv_row(row: int, key: str, i18n_key: str) -> None:
            lbl = ctk.CTkLabel(session_card, text_color=INK_DIM, anchor="w")
            self._i18n(lbl, i18n_key, size=11, bold=False)
            lbl.grid(row=row, column=0, sticky="w", padx=(12, 6), pady=0)
            value = ctk.CTkLabel(session_card, text="--", font=_FONT_MONO, text_color=INK, anchor="e")
            value.grid(row=row, column=1, sticky="e", padx=(6, 12), pady=0)
            self._kv_rows[key] = (lbl, value)
            self._value_labels[key] = value

        add_kv_row(0, "startexp", "kv_start_exp")
        add_kv_row(1, "expdiff", "kv_exp_diff")
        add_kv_row(2, "eta", "kv_eta")
        add_kv_row(3, "projexp", "kv_proj_exp")
        add_kv_row(4, "hploss", "kv_hp_loss")
        add_kv_row(5, "mploss", "kv_mp_loss")

        # Three buttons share one row: the left one cycles Pause/Resume/Start
        # depending on _run_state (see _on_pause_button_clicked), the middle
        # one is the unconditional manual Restart, the right one is the
        # unconditional manual Stop -- both hidden only in the "stopped"
        # state, where Start already covers beginning a new session and
        # there's nothing left running/paused to restart or stop.
        button_row = ctk.CTkFrame(parent, fg_color="transparent")
        button_row.grid(row=3, column=0, sticky="ew", padx=2, pady=(0, 2))
        button_row.grid_columnconfigure(0, weight=1)
        button_row.grid_columnconfigure(1, weight=1)
        button_row.grid_columnconfigure(2, weight=1)

        self._pause_button = ctk.CTkButton(
            button_row, command=self._on_pause_button_clicked,
            fg_color=SURFACE_2, hover_color=TRACK_BG, text_color=INK,
            corner_radius=9, height=BUTTON_HEIGHT,
        )
        self._pause_button.grid(row=0, column=0, sticky="ew", padx=(0, 3))

        self._restart_button = ctk.CTkButton(
            button_row, command=self._on_restart_clicked,
            fg_color=ACCENT, text_color=ACCENT_INK, hover_color="#7ff2e0",
            corner_radius=9, height=BUTTON_HEIGHT,
        )
        self._i18n(self._restart_button, "restart_button", size=13, bold=True)
        self._restart_button.grid(row=0, column=1, sticky="ew", padx=3)

        self._stop_button = ctk.CTkButton(
            button_row, command=self._on_stop_clicked,
            fg_color=SURFACE_2, hover_color=TRACK_BG, text_color=HP_COLOR,
            corner_radius=9, height=BUTTON_HEIGHT,
        )
        self._i18n(self._stop_button, "stop_button", size=13, bold=True)
        self._stop_button.grid(row=0, column=2, sticky="ew", padx=(3, 0))

    def _build_history_tab(self, parent) -> None:
        self._clear_history_button = ctk.CTkButton(
            parent, command=self._on_clear_history_clicked,
            fg_color=SURFACE_2, hover_color=TRACK_BG, text_color=HP_COLOR,
            corner_radius=9, height=28,
        )
        self._i18n(self._clear_history_button, "history_clear_button", size=11, bold=True)
        self._clear_history_button.pack(fill="x", padx=2, pady=(2, 4))

        self._history_frame = ctk.CTkScrollableFrame(parent, fg_color=BG, label_text="")
        self._history_frame.pack(fill="both", expand=True, padx=2, pady=(0, 2))
        self._history_empty_label = ctk.CTkLabel(
            self._history_frame, text_color=INK_FAINT,
        )
        self._i18n(self._history_empty_label, "history_empty", size=13, bold=False)
        self._history_empty_label.pack(pady=24)

    def _build_settings_tab(self, parent) -> None:
        # Scrollable: at some WINDOW SCALE values the settings content is
        # taller than the window, and a plain .pack() into the tab would
        # just clip the overflow with no way to reach it -- a scrollbar
        # keeps every option reachable regardless of scale/window size.
        scroll = ctk.CTkScrollableFrame(parent, fg_color=BG, label_text="")
        scroll.pack(fill="both", expand=True)

        window_card = ctk.CTkFrame(scroll, fg_color=SURFACE, corner_radius=12)
        window_card.pack(fill="x", padx=2, pady=(2, 3))

        # Value lives in the section header, not squeezed into the control
        # row -- at narrow window widths (esp. with the scrollbar eating
        # horizontal space) a fixed-width label at the end of a packed row
        # was getting clipped to invisible. The header always has room.
        self._scale_header_label = ctk.CTkLabel(
            window_card, text=self._scale_header_text(),
            anchor="w", text_color=INK_DIM, font=self._font(10, bold=True),
        )
        self._scale_header_label.pack(fill="x", padx=12, pady=(5, 0))
        scale_row = ctk.CTkFrame(window_card, fg_color="transparent")
        scale_row.pack(fill="x", padx=12, pady=(0, 3))
        # A +/- stepper instead of a slider -- a small draggable handle at
        # this widget size was fiddly to land on an exact value; discrete
        # SCALE_STEP_PCT taps are precise and don't need fine motor control.
        ctk.CTkButton(
            scale_row, text="-", width=36, command=lambda: self._on_scale_step(-SCALE_STEP_PCT),
            fg_color=SURFACE_2, hover_color=TRACK_BG, text_color=INK, font=_FONT_UI_BOLD,
        ).pack(side="left")
        ctk.CTkButton(
            scale_row, text="+", width=36, command=lambda: self._on_scale_step(SCALE_STEP_PCT),
            fg_color=SURFACE_2, hover_color=TRACK_BG, text_color=INK, font=_FONT_UI_BOLD,
        ).pack(side="left", padx=(6, 0))

        self._topmost_var = tk.BooleanVar(value=self._settings.topmost)
        self._i18n(ctk.CTkSwitch(
            window_card, variable=self._topmost_var, text_color=INK,
            progress_color=ACCENT, button_color=INK_DIM, button_hover_color=ACCENT,
            command=self._on_topmost_changed,
        ), "settings_always_on_top", size=11, bold=False).pack(fill="x", padx=12, pady=(0, 3))

        lang_row = ctk.CTkFrame(window_card, fg_color="transparent")
        lang_row.pack(fill="x", padx=12, pady=(0, 4))
        self._i18n(
            ctk.CTkLabel(lang_row, anchor="w", text_color=INK_DIM), "settings_language", size=10, bold=True
        ).pack(side="left")
        self._lang_button = ctk.CTkSegmentedButton(
            lang_row, values=["中文", "EN"], command=self._on_language_button_changed,
            selected_color=ACCENT, selected_hover_color="#7ff2e0", text_color=INK,
        )
        self._lang_button.set("中文" if self._settings.language == "zh" else "EN")
        self._lang_button.pack(side="right")

        card = ctk.CTkFrame(scroll, fg_color=SURFACE, corner_radius=12)
        card.pack(fill="x", padx=2, pady=(0, 0))

        self._interval_header_label = ctk.CTkLabel(
            card, text=self._interval_header_text(),
            anchor="w", text_color=INK_DIM, font=self._font(10, bold=True),
        )
        self._interval_header_label.pack(fill="x", padx=12, pady=(5, 0))
        slider_row = ctk.CTkFrame(card, fg_color="transparent")
        slider_row.pack(fill="x", padx=12, pady=(0, 2))
        slider = ctk.CTkSlider(
            slider_row, from_=1, to=60, number_of_steps=59, command=self._on_interval_changed,
            progress_color=ACCENT, button_color=ACCENT, button_hover_color="#7ff2e0",
        )
        slider.set(self._settings.window_min)
        slider.pack(fill="x", expand=True)

        self._i18n(
            ctk.CTkLabel(card, anchor="w", text_color=INK_DIM), "settings_display", size=10, bold=True
        ).pack(fill="x", padx=12, pady=(2, 0))

        self._switch_vars: dict[str, tk.BooleanVar] = {}
        for key, i18n_key, attr in (
            ("hp", "settings_show_hp", "show_hp"),
            ("mp", "settings_show_mp", "show_mp"),
            ("exp", "settings_show_exp", "show_exp"),
            ("exp_pct", "settings_show_exp_pct", "show_exp_pct"),
            ("eta", "settings_show_eta", "show_eta"),
            ("proj_exp", "settings_show_proj_exp", "show_proj_exp"),
        ):
            var = tk.BooleanVar(value=getattr(self._settings, attr))
            self._switch_vars[key] = var
            self._i18n(ctk.CTkSwitch(
                card, variable=var, text_color=INK,
                progress_color=ACCENT, button_color=INK_DIM, button_hover_color=ACCENT,
                command=lambda k=key, a=attr, v=var: self._on_switch_changed(k, a, v),
            ), i18n_key, size=11, bold=False).pack(fill="x", padx=12, pady=0)

        # SESSION: behaviour switches, not display toggles -- neither one
        # hides/shows a widget, so they bypass _on_switch_changed/_apply_visibility
        # entirely (see _on_auto_stop_changed/_on_save_on_restart_changed).
        self._i18n(
            ctk.CTkLabel(card, anchor="w", text_color=INK_DIM), "settings_session", size=10, bold=True
        ).pack(fill="x", padx=12, pady=(3, 0))

        self._auto_stop_var = tk.BooleanVar(value=self._settings.auto_stop)
        self._i18n(ctk.CTkSwitch(
            card, variable=self._auto_stop_var, text_color=INK,
            progress_color=ACCENT, button_color=INK_DIM, button_hover_color=ACCENT,
            command=self._on_auto_stop_changed,
        ), "settings_auto_stop", size=11, bold=False).pack(fill="x", padx=12, pady=0)

        self._save_on_restart_var = tk.BooleanVar(value=self._settings.save_on_restart)
        self._i18n(ctk.CTkSwitch(
            card, variable=self._save_on_restart_var, text_color=INK,
            progress_color=ACCENT, button_color=INK_DIM, button_hover_color=ACCENT,
            command=self._on_save_on_restart_changed,
        ), "settings_save_on_restart", size=11, bold=False).pack(fill="x", padx=12, pady=(0, 4))

    # ---- settings callbacks ------------------------------------------------

    def _on_scale_step(self, delta: int) -> None:
        pct = max(SCALE_MIN_PCT, min(SCALE_MAX_PCT, self._settings.scale_pct + delta))
        if pct == self._settings.scale_pct:
            return
        self._settings.scale_pct = pct
        self._scale_header_label.configure(text=self._scale_header_text())
        # CTk's own scaling knobs: widget_scaling resizes fonts/padding/etc,
        # window_scaling resizes the geometry set via .geometry() -- both are
        # needed together, otherwise widgets end up mismatched against the
        # window size. Both apply live to the already-open root window.
        factor = pct / 100
        ctk.set_widget_scaling(factor)
        ctk.set_window_scaling(factor)

    def _on_topmost_changed(self) -> None:
        self._settings.topmost = self._topmost_var.get()
        self.root.attributes("-topmost", self._settings.topmost)

    def _on_language_button_changed(self, value: str) -> None:
        self._apply_language("zh" if value == "中文" else "en")

    def _on_interval_changed(self, value: float) -> None:
        self._settings.window_min = round(value)
        self._interval_header_label.configure(text=self._interval_header_text())
        # Doesn't retroactively affect the currently-running session's
        # already-baked-in target -- takes effect for the *next* session,
        # same as the interval_minutes recorded on SessionSummary.finalize().

    def _on_switch_changed(self, key: str, attr: str, var: tk.BooleanVar) -> None:
        setattr(self._settings, attr, var.get())
        if key != "exp_pct":  # visibility-affecting; exp_pct only changes rendered text
            self._apply_visibility()
        self._render(self._last)  # immediate feedback

    def _on_auto_stop_changed(self) -> None:
        self._settings.auto_stop = self._auto_stop_var.get()

    def _on_save_on_restart_changed(self) -> None:
        self._settings.save_on_restart = self._save_on_restart_var.get()

    def _apply_visibility(self) -> None:
        s = self._settings
        visible_stats = {"level": True, "hp": s.show_hp, "mp": s.show_mp, "exp": s.show_exp}
        for key, (lbl, bar, value) in self._stat_rows.items():
            widgets = [lbl, value] + ([bar] if bar else [])
            for w in widgets:
                w.grid() if visible_stats[key] else w.grid_remove()

        visible_kv = {
            "startexp": True, "expdiff": True, "eta": s.show_eta,
            "projexp": s.show_proj_exp, "hploss": s.show_hp, "mploss": s.show_mp,
        }
        for key, (lbl, value) in self._kv_rows.items():
            for w in (lbl, value):
                w.grid() if visible_kv[key] else w.grid_remove()

    # ---- tick loop ---------------------------------------------------------

    def _tick(self) -> None:
        # Every path through this method must reschedule -- this loop is the
        # only thing driving the HUD, so an exception escaping before
        # self.root.after(...) freezes it permanently on stale data. The
        # window itself stays responsive, which makes the failure especially
        # confusing: buttons still click, Restart Session still "works", and
        # nothing ever updates again.
        #
        # Hence both the except *and* the finally. The original try/except
        # wasn't enough on its own: in the release .exe an unencodable OCR
        # character raised UnicodeEncodeError out of _do_tick's debug print,
        # and the handler's own `print(... {e!r})` re-raised on the same
        # unencodable text, so the reschedule below was never reached (see
        # the stdout-sink note at the top of this module for that trigger's
        # actual fix, and _log for why logging can no longer raise at all).
        # `finally` is what makes the loop survive the *next* such bug.
        next_delay = TARGET_MS
        try:
            next_delay = self._do_tick()
        except Exception as e:
            self._log(f"[{time.strftime('%H:%M:%S')}] tick error: {e!r}")
            with contextlib.suppress(Exception):
                self._set_status_error(self._t("status_error_unknown", detail=str(e)))
        finally:
            self.root.after(next_delay, self._tick)

    @staticmethod
    def _log(message: str) -> None:
        """Debug logging must never be able to kill the tick loop -- it is the
        least important thing this app does and has already taken the whole
        HUD down once (see _tick)."""
        with contextlib.suppress(Exception):
            print(message, flush=True)

    def _do_tick(self) -> int:
        t0 = time.perf_counter()
        try:
            field_images = self._source.grab_fields()
        except RuntimeError as e:
            # Game window gone (closed/crashed), minimized, or the stat panel
            # is covered by another window -- don't crash the HUD, show it
            # plainly and keep retrying at a slower pace in case it clears.
            #
            # Logged on *transition* only: this path produces no other output,
            # so a persistently obscured panel used to leave a completely
            # empty log with nothing to diagnose from -- but logging every
            # 2s retry would bury the real ticks.
            if str(e) != self._last_capture_error:
                self._log(f"[{time.strftime('%H:%M:%S')}] capture unavailable: {e}")
                self._last_capture_error = str(e)
            self._set_status_error(self._localize_error(str(e)))
            # The session clock is wall-clock time (Session.elapsed()), not
            # tick-driven, so it keeps running even while OCR can't read the
            # panel (game window covered, alt-tabbed away, minimized). Both
            # of these used to be skipped entirely on this path: the timer
            # chip froze at its last-rendered text even though the real
            # countdown kept going underneath, and a session whose window
            # stayed blocked past its interval would never auto-finalize at
            # all, silently overrunning forever.
            self._update_timer_label()
            self._maybe_finalize_on_timeout()
            return 2000
        if self._last_capture_error is not None:
            self._log(f"[{time.strftime('%H:%M:%S')}] capture recovered")
            self._last_capture_error = None

        # Every crop is scaled from the client size (regions.py), so a log
        # without it can't explain a bad read -- and a mid-session resize is
        # exactly the kind of thing that moves the panel out from under the
        # boxes. Logged once at startup and again on any change.
        client_size = getattr(self._source, "client_size", None)
        if client_size is not None and client_size != self._last_client_size:
            self._log(f"[{time.strftime('%H:%M:%S')}] client size: {client_size[0]}x{client_size[1]}")
            self._last_client_size = client_size
        field_text = {name: self._ocr.read_field(img) for name, img in field_images.items()}
        snap = parse_fields(field_text)
        self._log(f"[{time.strftime('%H:%M:%S')}] fields={field_text}")
        self._log(f"          -> {snap}")
        # A single tick occasionally misses a field (combat effects/floating
        # damage numbers over the HP/MP bars, transient OCR confidence dips) --
        # observed live: HP briefly read as None while MP/EXP/LV parsed fine on
        # the same frame. Carry forward the last known value per field instead
        # of flickering to '--' on every miss; a field that's genuinely gone
        # (e.g. OCR permanently broken) will just show stale data, which is a
        # more honest failure mode than a blank field for a live number.
        merged = StatSnapshot(*(
            new if new is not None else old
            for new, old in zip(vars(snap).values(), vars(self._last).values())
        ))
        self._last = merged
        # hp_max/mp_max are passed purely so Session can sanity-check them --
        # a tick whose max doesn't match the rest of the session was misparsed
        # (see rate.py's _LossTracker) and is dropped before it can inflate the
        # loss totals.
        #
        # There used to be a "does this frame even look like the stat panel?"
        # gate here (reject the tick unless LV parsed). It was removed after
        # ablating it against both live captures: it changed the totals by
        # exactly zero, because rate.py already rejects those same frames one
        # layer down -- and it carried a real risk of its own, since a broken
        # LV crop would have stopped a session recording anything at all.
        # tests/test_captured_regression.py replays the real failure through
        # this path with no gate in front of it.
        # Gated on run_state rather than relying on Session's own pause/no-op
        # behaviour: while "stopped" the Session may never have been started
        # at all (see _run_state's docstring in __init__), and feeding it
        # ticks here would silently begin calibrating/tracking a session the
        # user hasn't asked for yet.
        if self._run_state == "running":
            self._session.record(
                merged.exp_cur, merged.hp_cur, merged.mp_cur, merged.exp_pct,
                hp_max=merged.hp_max, mp_max=merged.mp_max, level=merged.level,
            )

        self._maybe_finalize_on_timeout()

        self._render(merged)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return max(0, int(TARGET_MS - elapsed_ms))

    def _update_timer_label(self) -> None:
        """Split out of _render so the capture-error path in _do_tick can
        keep the countdown moving without running a full render against
        stale/absent OCR data."""
        if self._run_state == "stopped":
            # A stopped session (including the very first one, before Start
            # is ever clicked) has no countdown running -- showing a static
            # "10:00" the whole time would look like a stuck timer rather
            # than a genuinely inactive one.
            self._timer_label.configure(text="--:--")
            return
        remaining = max(0.0, self._settings.window_min * 60 - self._session.elapsed())
        remaining_s = f"{int(remaining // 60)}:{int(remaining % 60):02d}"
        self._timer_label.configure(text=self._t("timer_left", time=remaining_s))

    def _maybe_finalize_on_timeout(self) -> None:
        # Skipped while a rename dialog is open: simpledialog.askstring blocks
        # via a nested Tk event loop but doesn't stop self.root.after() timers
        # from firing, so without this guard a session could finalize and
        # insert a new history card underneath the open modal mid-edit. Also
        # skipped outright unless actually running: elapsed() is frozen while
        # paused/stopped anyway, so this wouldn't fire either way, but being
        # explicit here means it can't ever race a state change mid-tick.
        #
        # Called from both branches of _do_tick (capture success and capture
        # failure) -- Session.elapsed() is wall-clock time, not tick-driven,
        # so a session must still be able to hit its interval and finalize
        # even while the game window is covered/minimized for the whole
        # window, not just while OCR happens to be succeeding.
        if not self._modal_open and self._run_state == "running" \
                and self._session.elapsed() >= self._settings.window_min * 60:
            self._finalize_and_maybe_stop()

    def _commit_session_to_history(self) -> None:
        # Shared by the timer rollover and a manual restart with
        # save_on_restart on -- exactly one code path commits, so two
        # triggers landing on the same tick can't double-log.
        # Skip logging if the session never got a real EXP reading (restart
        # clicked immediately after launch, before OCR produced anything --
        # a '? -> ?' entry would just be noise), or if essentially no time
        # passed (rapid double-click on the restart button after real data
        # already exists -- start() carries the last known values forward,
        # so a second click 50ms later would otherwise log a valid-looking
        # but meaningless 0-duration, 0-diff entry).
        if self._session.start_exp is not None and self._session.elapsed() >= 1.0:
            summary = self._session.finalize(self._settings.window_min)
            self._session_history.append(summary)
            self._log(f"[{time.strftime('%H:%M:%S')}] {_fmt_summary(summary, len(self._session_history))}")
            self._append_history_card(summary, len(self._session_history))

    def _finalize_and_maybe_stop(self) -> None:
        """The timer rolling over. Always commits to History first; then
        either stops (default -- see settings.auto_stop) or immediately
        starts the next session, the only behaviour before that setting
        existed."""
        self._commit_session_to_history()
        if self._settings.auto_stop:
            # Reuses Session.pause() rather than adding a third Session
            # state: it freezes elapsed() at exactly this instant and makes
            # record() a no-op, which is exactly what "stopped" needs, and
            # nothing else in rate.py has to know "stopped" exists.
            self._session.pause()
            self._run_state = "stopped"
            self._apply_run_state()
        else:
            self._session.start()

    def _on_restart_clicked(self) -> None:
        if self._settings.save_on_restart:
            self._commit_session_to_history()
        # Restart is reachable while "paused" (see _apply_run_state), during
        # which record() -- and with it self._session's cached cur values --
        # has been a no-op the whole time. Without this sync, start() below
        # would baseline off however-stale a reading was current when Pause
        # was clicked instead of the latest one on screen right now.
        self._session.sync_current(self._last.exp_cur, self._last.hp_cur, self._last.mp_cur)
        self._session.start()  # resets pause state too, so a restart from "paused" lands in "running"
        self._run_state = "running"
        self._apply_run_state()
        self._render(self._last)  # immediate feedback, don't wait for next tick

    def _on_stop_clicked(self) -> None:
        """Manual equivalent of the timer rolling over with auto_stop on
        (see _finalize_and_maybe_stop): always commits to History -- unlike
        Restart, there's no "next session" being discarded here for
        save_on_restart's opt-out to apply to -- then freezes the session via
        pause() exactly the way auto-stop does, reusing "paused" as the
        underlying state for "stopped" rather than adding a third one (see
        that method's own comment)."""
        self._commit_session_to_history()
        self._session.pause()
        self._run_state = "stopped"
        self._apply_run_state()
        self._render(self._last)  # immediate feedback, don't wait for next tick

    def _on_pause_button_clicked(self) -> None:
        """One button, three roles depending on _run_state -- see
        _apply_run_state for how its label/command follow that state."""
        if self._run_state == "running":
            self._session.pause()
            self._run_state = "paused"
        elif self._run_state == "paused":
            self._session.resume()
            self._run_state = "running"
        else:  # "stopped" -- already committed to History by _finalize_and_maybe_stop
            # Same staleness as _on_restart_clicked -- record() has been gated
            # off the whole time the session sat "stopped".
            self._session.sync_current(self._last.exp_cur, self._last.hp_cur, self._last.mp_cur)
            self._session.start()
            self._run_state = "running"
        self._apply_run_state()
        self._render(self._last)  # immediate feedback, don't wait for next tick

    def _apply_run_state(self) -> None:
        label_key = {"running": "pause_button", "paused": "resume_button", "stopped": "start_button"}[self._run_state]
        self._pause_button.configure(text=self._t(label_key), font=self._font(12, bold=True))
        # A Restart with nothing running/paused to restart from doesn't mean
        # anything -- Start (the pause button's role while stopped) already
        # covers beginning the next session. As the sole button in the row
        # it's centered and shrunk rather than stretched across both
        # columns the way the two-button running/paused layout is.
        if self._run_state == "stopped":
            self._restart_button.grid_remove()
            self._stop_button.grid_remove()
            self._pause_button.configure(width=STOPPED_BUTTON_WIDTH, height=BUTTON_HEIGHT)
            self._pause_button.grid(row=0, column=0, columnspan=3, sticky="", padx=0)
        else:
            self._pause_button.configure(width=140, height=BUTTON_HEIGHT)  # CTkButton's own default width
            self._pause_button.grid(row=0, column=0, columnspan=1, sticky="ew", padx=(0, 3))
            self._restart_button.grid(row=0, column=1, columnspan=1, sticky="ew", padx=3)
            self._stop_button.grid(row=0, column=2, columnspan=1, sticky="ew", padx=(3, 0))

    def _rebuild_history_cards(self) -> None:
        for card in self._history_cards:
            card.destroy()
        self._history_cards.clear()
        if not self._session_history:
            # _append_history_card only ever pack_forget()s this label (on
            # the first card added) -- nothing re-packs it once the list is
            # emptied again (e.g. via Clear History), so do it explicitly.
            self._history_empty_label.pack(pady=24)
            return
        # Cards are always inserted at the top (newest-first) -- rebuilding
        # oldest-first via _append_history_card reproduces the exact same
        # final order without needing separate "rebuild" layout logic.
        for index, summary in enumerate(self._session_history, start=1):
            self._append_history_card(summary, index)

    def _append_history_card(self, summary: SessionSummary, index: int) -> None:
        self._history_empty_label.pack_forget()

        card = ctk.CTkFrame(self._history_frame, fg_color=SURFACE, corner_radius=10)
        # Newest-first: pack before the current top card (if any) rather than
        # appending, so the most recently finalized session is always the
        # first thing visible in the scrollable frame.
        if self._history_cards:
            card.pack(fill="x", pady=(0, 8), before=self._history_cards[0])
        else:
            card.pack(fill="x", pady=(0, 8))
        self._history_cards.insert(0, card)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(10, 0))
        title_label = ctk.CTkLabel(
            head, text=summary.name or self._t("history_session", n=index), font=self._font(10, bold=True),
            text_color=INK_FAINT, cursor="hand2",
        )
        title_label.pack(side="left")
        title_label.bind("<Button-1>", lambda _e, i=index, lbl=title_label: self._on_rename_clicked(i, lbl))

        # Packed before dur_text below so it lands rightmost -- pack(side="right")
        # stacks from the outer edge inward in packing order, so whichever
        # side="right" widget is packed first ends up furthest right.
        ctk.CTkButton(
            head, text="×", width=22, height=18, command=lambda i=index: self._on_delete_history_clicked(i),
            fg_color="transparent", hover_color=SURFACE_2, text_color=INK_FAINT, font=_FONT_UI_BOLD,
        ).pack(side="right")

        dur_min = summary.duration_s / 60
        # Mixes translated chrome ("restarted early"/提前重啟) with the
        # duration number when applicable, so this needs the language-aware
        # font -- the plain "10.0m" case doesn't strictly need it, but the
        # widget is rebuilt wholesale on language switch anyway either way.
        unit = self._t("unit_min_short")
        if summary.interval_minutes is not None and abs(dur_min - summary.interval_minutes) > 0.05:
            dur_text = self._t(
                "history_duration_early",
                dur=f"{dur_min:.1f}",
                target=summary.interval_minutes,
                unit=unit,
                label=self._t("history_restarted_early"),
            )
            dur_color = EXP_COLOR
            dur_font = self._font(11)
        else:
            dur_text, dur_color, dur_font = f"{dur_min:.1f}{unit}", INK_DIM, _FONT_MONO_SM
        ctk.CTkLabel(head, text=dur_text, font=dur_font, text_color=dur_color).pack(side="right")

        timestamp = ctk.CTkFrame(card, fg_color="transparent")
        timestamp.pack(fill="x", padx=12, pady=(0, 4))
        start_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(summary.start_time))
        end_ts = time.strftime("%H:%M:%S", time.localtime(summary.end_time))
        ctk.CTkLabel(
            timestamp, text=f"{start_ts} → {end_ts}", font=_FONT_MONO_SM, text_color=INK_FAINT,
        ).pack(side="left")

        rng = ctk.CTkFrame(card, fg_color="transparent")
        rng.pack(fill="x", padx=12, pady=(0, 8))
        start_s = f"{summary.start_exp:,}" if summary.start_exp is not None else "?"
        end_s = f"{summary.end_exp:,}" if summary.end_exp is not None else "?"
        diff = summary.exp_diff
        diff_s = f"+{diff:,}" if diff is not None else "?"
        pct_diff = summary.exp_pct_diff
        ctk.CTkLabel(rng, text=start_s, font=_FONT_MONO, text_color=INK).pack(side="left")
        ctk.CTkLabel(rng, text=" → ", font=_FONT_MONO, text_color=INK_FAINT).pack(side="left")
        ctk.CTkLabel(rng, text=end_s, font=_FONT_MONO, text_color=INK).pack(side="left")
        ctk.CTkLabel(rng, text=f"  {diff_s}", font=_FONT_MONO, text_color=EXP_COLOR).pack(side="left")
        if pct_diff is not None:
            ctk.CTkLabel(rng, text=f" (+{pct_diff:.2f}%)", font=_FONT_MONO_SM, text_color=INK_DIM).pack(side="left")

        mini = ctk.CTkFrame(card, fg_color="transparent")
        mini.pack(fill="x", padx=12, pady=(0, 10))
        mini.grid_columnconfigure((0, 1), weight=1, uniform="mini")

        def mini_stat(col: int, label: str, value: str, color: str) -> None:
            box = ctk.CTkFrame(mini, fg_color=SURFACE_2, corner_radius=7)
            box.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 4, 0))
            ctk.CTkLabel(box, text=label, font=self._font(9, bold=True), text_color=INK_FAINT, anchor="w").pack(
                fill="x", padx=8, pady=(6, 0)
            )
            ctk.CTkLabel(box, text=value, font=_FONT_MONO_SM, text_color=color, anchor="w").pack(
                fill="x", padx=8, pady=(0, 6)
            )

        mini_stat(0, self._t("history_hp_loss"), _fmt_loss(summary.hp_loss), HP_COLOR if summary.hp_loss > 0 else INK_FAINT)
        mini_stat(1, self._t("history_mp_loss"), _fmt_loss(summary.mp_loss), MP_COLOR if summary.mp_loss > 0 else INK_FAINT)

    @contextlib.contextmanager
    def _modal(self):
        """Run a blocking dialog. Two things have to happen around one:

        1. `_modal_open` tells _do_tick not to finalize a session while a
           dialog is up -- askstring/askyesno block on a *nested* Tk event
           loop, which does not stop self.root.after() timers from firing,
           so a session could otherwise roll over and insert a history card
           underneath the open modal mid-edit.
        2. -topmost has to come off for the duration. Tk dialogs are not
           topmost themselves, so with the HUD pinned above everything the
           dialog renders *behind* it -- while still holding a grab on all
           input. The app looks frozen (clicks on the HUD, including Restart
           Session, do nothing) with no visible cause, and stays that way
           until the invisible dialog is found and dismissed.
        """
        self._modal_open = True
        was_topmost = self._settings.topmost
        if was_topmost:
            self.root.attributes("-topmost", False)
        try:
            yield
        finally:
            self._modal_open = False
            if was_topmost:
                self.root.attributes("-topmost", True)

    def _on_rename_clicked(self, index: int, label: ctk.CTkLabel) -> None:
        # index is 1-based. session_history is no longer strictly append-only
        # (see _on_delete_history_clicked), but deleting any entry rebuilds
        # every card from scratch via _rebuild_history_cards(), so a *live*
        # card's index - 1 is always still correct: it can only go stale by
        # having its own card destroyed and recreated with the new one first.
        current = self._session_history[index - 1]
        with self._modal():
            new_name = simpledialog.askstring(
                self._t("rename_dialog_title"), self._t("rename_dialog_prompt"),
                initialvalue=current.name or self._t("history_session", n=index),
                parent=self.root,
            )
        if new_name is None:
            return  # cancelled
        new_name = new_name.strip()
        updated = dataclasses.replace(current, name=new_name or None)
        self._session_history[index - 1] = updated
        label.configure(text=updated.name or self._t("history_session", n=index))

    def _on_delete_history_clicked(self, index: int) -> None:
        summary = self._session_history[index - 1]
        with self._modal():  # see _do_tick's guard comment on _modal()
            confirmed = messagebox.askyesno(
                self._t("history_delete_confirm_title"),
                self._t(
                    "history_delete_confirm_prompt",
                    name=summary.name or self._t("history_session", n=index),
                ),
                parent=self.root,
            )
        if not confirmed:
            return
        del self._session_history[index - 1]
        # Every remaining card's 1-based index shifts once one entry is
        # removed -- rebuild from scratch rather than patching indices in
        # place, same as _on_clear_history_clicked already does.
        self._rebuild_history_cards()

    def _on_clear_history_clicked(self) -> None:
        if not self._session_history:
            return
        with self._modal():  # see _do_tick's guard comment on _modal()
            confirmed = messagebox.askyesno(
                self._t("history_clear_confirm_title"),
                self._t("history_clear_confirm_prompt", n=len(self._session_history)),
                parent=self.root,
            )
        if not confirmed:
            return
        self._session_history.clear()
        self._rebuild_history_cards()

    def _set_status_error(self, text: str) -> None:
        self._status_pill.configure(text=text, fg_color=SURFACE_2, text_color=HP_COLOR)

    # ---- render --------------------------------------------------------

    def _render(self, snap: StatSnapshot) -> None:
        self._value_labels["level"].configure(text=str(snap.level) if snap.level is not None else "--")

        if snap.hp_cur is not None:
            self._value_labels["hp"].configure(text=f"{snap.hp_cur}/{snap.hp_max}")
            if snap.hp_max:
                self._bars["hp"].set(max(0.0, min(1.0, snap.hp_cur / snap.hp_max)))
        else:
            self._value_labels["hp"].configure(text="--")

        if snap.mp_cur is not None:
            self._value_labels["mp"].configure(text=f"{snap.mp_cur}/{snap.mp_max}")
            if snap.mp_max:
                self._bars["mp"].set(max(0.0, min(1.0, snap.mp_cur / snap.mp_max)))
        else:
            self._value_labels["mp"].configure(text="--")

        pct = f"  ({snap.exp_pct:.2f}%)" if snap.exp_pct is not None and self._settings.show_exp_pct else ""
        if snap.exp_cur is not None:
            self._value_labels["exp"].configure(text=f"{snap.exp_cur:,}{pct}")
            if snap.exp_pct is not None:
                self._bars["exp"].set(max(0.0, min(1.0, snap.exp_pct / 100)))
        else:
            self._value_labels["exp"].configure(text="--")

        start_exp = self._session.start_exp
        self._value_labels["startexp"].configure(text=f"{start_exp:,}" if start_exp is not None else "--")

        self._update_timer_label()

        exp_diff = self._session.exp_diff
        # Total EXP required for the current level isn't shown directly by the
        # game, but can be derived from any single tick that has both the
        # absolute value and percentage: total = cur / (pct/100). Anchoring
        # off the current tick (rather than diffing OCR'd percentages
        # directly) is more robust since per-level EXP totals are constant,
        # while independently-read percentages carry their own OCR noise on
        # top of the cur value's.
        total_exp = snap.exp_cur / (snap.exp_pct / 100) if snap.exp_cur and snap.exp_pct else None

        if exp_diff is not None:
            pct_s = f"  (+{exp_diff / total_exp * 100:.2f}%)" if total_exp and self._settings.show_exp_pct else ""
            self._value_labels["expdiff"].configure(text=f"+{exp_diff:,}{pct_s}")
        else:
            self._value_labels["expdiff"].configure(text="--")

        # ETA to level up: current session's EXP/sec rate, projected against
        # the EXP still needed (total - cur). Needs a few seconds of session
        # data first -- extrapolating off a 1-2 second sample swings wildly.
        elapsed = self._session.elapsed()
        eta_s = None
        if exp_diff and exp_diff > 0 and elapsed > 3 and total_exp and snap.exp_cur:
            rate_per_sec = exp_diff / elapsed
            remaining_exp = total_exp - snap.exp_cur
            if rate_per_sec > 0:
                eta_s = remaining_exp / rate_per_sec
        self._value_labels["eta"].configure(text=_fmt_duration(eta_s) if eta_s is not None else "--")

        # Projected session total: current rate extrapolated across the full
        # window setting, not just what's elapsed so far -- see
        # Session.projected_exp (same 3s/positive-gain guard as ETA above,
        # for the same reason).
        proj = self._session.projected_exp(self._settings.window_min * 60)
        if proj is not None:
            proj_pct_s = f"  (+{proj / total_exp * 100:.2f}%)" if total_exp and self._settings.show_exp_pct else ""
            self._value_labels["projexp"].configure(text=f"+{proj:,}{proj_pct_s}")
        else:
            self._value_labels["projexp"].configure(text="--")

        hp_loss, mp_loss = self._session.hp_loss, self._session.mp_loss
        self._value_labels["hploss"].configure(
            text=_fmt_loss(hp_loss), text_color=HP_COLOR if hp_loss > 0 else INK_FAINT
        )
        self._value_labels["mploss"].configure(
            text=_fmt_loss(mp_loss), text_color=MP_COLOR if mp_loss > 0 else INK_FAINT
        )

        # Pause/stop/calibration are user- or engine-driven states that take
        # priority over the activity-based idle/tracking read below -- e.g. a
        # paused session with real HP/MP/EXP movement in its history isn't
        # "Idle", it's "Paused".
        if self._run_state == "paused":
            self._status_pill.configure(text=self._t("status_paused"), fg_color=SURFACE_2, text_color=EXP_COLOR)
        elif self._run_state == "stopped":
            self._status_pill.configure(text=self._t("status_stopped"), fg_color=SURFACE_2, text_color=INK_DIM)
        elif self._session.is_calibrating:
            self._status_pill.configure(text=self._t("status_calibrating"), fg_color=SURFACE_2, text_color=EXP_COLOR)
        else:
            # Idle only if NONE of HP/MP/EXP have changed recently within this
            # session -- any one of them moving counts as activity, not idle.
            idle = hp_loss == 0 and mp_loss == 0 and (exp_diff or 0) == 0
            if idle:
                self._status_pill.configure(text=self._t("status_idle"), fg_color=SURFACE_2, text_color=INK_DIM)
            else:
                self._status_pill.configure(text=self._t("status_tracking"), fg_color=TRACK_BG, text_color=OK_COLOR)

    def run(self) -> None:
        self.root.mainloop()
