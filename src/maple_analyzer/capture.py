"""Window capture abstraction.

`WindowCapture.grab_panel()` returns a PIL image of just the stat panel region.
Two implementations:

- `GameWindowCapture` (Windows only): finds the MapleStory window by title via
  pywin32, reads its client rect, grabs that rect off screen with `mss`. This is
  the real production path.
- `StaticImageCapture` (any platform): replays a single image file every call.
  Used for dev/testing on machines without the game running (e.g. this repo's
  Linux dev environment) -- proves out the OCR/parse/rate/overlay code without
  needing Windows + a live client.

Call `get_capture()` to get whichever is appropriate for the current platform;
pass `sample_path=` to force StaticImageCapture regardless of platform (useful
for demos/tests on Windows too).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol

from PIL import Image

from .regions import field_boxes, panel_box


# Raised (as a RuntimeError message) when another window sits over the stat
# panel. Routine and recoverable, so it travels the same path as the
# minimized/not-found states -- see overlay._do_tick and _localize_error.
PANEL_OBSCURED = "stat panel is obscured"


def field_sample_points(client_size: tuple[int, int]) -> list[tuple[int, int]]:
    """Client-relative points to probe for occlusion: the four corners of each
    FIELD_BOX, inset by a pixel so a corner lands inside its own box.

    Sampling per *field* rather than the panel as a whole is deliberate. A
    window clipping only the MP digits is the dangerous case -- the 'MP' label
    stays readable so the value still parses, just wrong -- and a panel-level
    check with a few points can miss it.
    """
    points: list[tuple[int, int]] = []
    for b in field_boxes(client_size).values():
        points += [
            (b.left + 1, b.top + 1), (b.right - 2, b.top + 1),
            (b.left + 1, b.bottom - 2), (b.right - 2, b.bottom - 2),
        ]
    return points


def panel_is_obscured(sample_points, game_hwnd: int, window_at) -> bool:
    """True if any sample point belongs to a window other than the game.

    `window_at(x, y)` returns the *root* window at a screen point; injected so
    this stays testable without a Win32 desktop. Any single covered point
    counts -- there is no threshold, because partial coverage corrupts values
    rather than merely hiding them.
    """
    return any(window_at(x, y) != game_hwnd for x, y in sample_points)


class WindowCapture(Protocol):
    def grab_full(self) -> Image.Image:
        """Full client-area frame."""
        ...

    def grab_panel(self) -> Image.Image:
        """Just the stat panel crop, scaled to the current client size."""
        ...

    def grab_fields(self) -> dict[str, Image.Image]:
        """One crop per field_boxes() entry ('LV'/'HP'/'MP'/'EXP'), for
        recognition-only OCR -- see ocr.py's read_field()."""
        ...


class StaticImageCapture:
    """Dev/demo stand-in: always returns (a copy of) one image from disk."""

    def __init__(self, path: str | Path):
        self._image = Image.open(path).convert("RGB")

    def grab_full(self) -> Image.Image:
        return self._image.copy()

    def grab_panel(self) -> Image.Image:
        return self._image.crop(panel_box(self._image.size).as_tuple())

    def grab_fields(self) -> dict[str, Image.Image]:
        return {
            name: self._image.crop(box.as_tuple())
            for name, box in field_boxes(self._image.size).items()
        }


class GameWindowCapture:
    """Real capture: locates the game window by title and grabs its client area."""

    def __init__(self, title_substring: str = "新楓之谷", process_name: str = "Maplestory"):
        if sys.platform != "win32":
            raise RuntimeError("GameWindowCapture requires Windows (pywin32 + real desktop)")
        import mss
        import win32api
        import win32con
        import win32gui
        import win32process

        self._win32gui = win32gui
        self._win32process = win32process
        self._win32api = win32api
        self._win32con = win32con
        self._mss = mss.mss()
        self._title_substring = title_substring
        self._process_name = process_name.lower()
        self._hwnd: int | None = None
        # Last client size seen by grab_fields, for the overlay to log. Every
        # crop in regions.py is scaled from this, so it is the single most
        # useful number when diagnosing a bad read from a log after the fact
        # -- and the one thing missing from every capture taken so far.
        self.client_size: tuple[int, int] | None = None

    def _owning_process_name(self, hwnd: int) -> str:
        # Title alone isn't a reliable match: e.g. a browser tab for a wiki page
        # about the game can also contain the title substring. Require the
        # window's actual owning process to match too.
        try:
            _, pid = self._win32process.GetWindowThreadProcessId(hwnd)
            # PROCESS_VM_READ is denied for this game (anti-tamper protection) --
            # PROCESS_QUERY_LIMITED_INFORMATION alone is enough for
            # GetModuleFileNameEx and works even on protected processes.
            handle = self._win32api.OpenProcess(
                self._win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            try:
                path = self._win32process.GetModuleFileNameEx(handle, 0)
                return path.rsplit("\\", 1)[-1]
            finally:
                self._win32api.CloseHandle(handle)
        except Exception:
            return ""

    def _is_match(self, hwnd: int) -> bool:
        title = self._win32gui.GetWindowText(hwnd)
        if self._title_substring not in title:
            return False
        return self._process_name in self._owning_process_name(hwnd).lower()

    def _find_window(self) -> int:
        # IsWindow() alone isn't enough: if the game process exits, Windows can
        # recycle its hwnd number for an unrelated window, and IsWindow() stays
        # True for that new window -- silently capturing garbage instead of
        # erroring. Re-check title+process on every call to catch that.
        if self._hwnd and self._win32gui.IsWindow(self._hwnd) and self._is_match(self._hwnd):
            return self._hwnd
        self._hwnd = None

        found: list[int] = []

        def _cb(hwnd: int, _):
            if self._is_match(hwnd):
                found.append(hwnd)

        self._win32gui.EnumWindows(_cb, None)
        if not found:
            raise RuntimeError(f"No window found with title containing {self._title_substring!r}")
        self._hwnd = found[0]
        return self._hwnd

    def _client_rect_on_screen(self) -> tuple[int, int, int, int]:
        hwnd = self._find_window()
        if self._win32gui.IsIconic(hwnd):
            # Minimized windows report a client rect around (-32000, -32000)
            # with zero size -- mss.grab() throws a raw ScreenShotError on
            # that instead of anything actionable. Fail clearly here so
            # callers (the overlay) can show 'game minimized' and retry,
            # same as the 'game not found' case.
            raise RuntimeError("game window is minimized")
        left, top, right, bottom = self._win32gui.GetClientRect(hwnd)
        left, top = self._win32gui.ClientToScreen(hwnd, (left, top))
        right, bottom = self._win32gui.ClientToScreen(hwnd, (right, bottom))
        return left, top, right, bottom

    def grab_full(self) -> Image.Image:
        left, top, right, bottom = self._client_rect_on_screen()
        shot = self._mss.grab({"left": left, "top": top, "width": right - left, "height": bottom - top})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def grab_panel(self) -> Image.Image:
        left, top, right, bottom = self._client_rect_on_screen()
        client_size = (right - left, bottom - top)
        box = panel_box(client_size)
        shot = self._mss.grab({
            "left": left + box.left,
            "top": top + box.top,
            "width": box.right - box.left,
            "height": box.bottom - box.top,
        })
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def _root_window_at(self, x: int, y: int) -> int:
        # GA_ROOT (2) resolves a child window/control to its top-level owner,
        # so the game's own children don't read as something covering it.
        return self._win32gui.GetAncestor(self._win32gui.WindowFromPoint((x, y)), 2)

    def grab_fields(self) -> dict[str, Image.Image]:
        # One screen grab covering the whole panel (mss itself is cheap, ~3.5ms
        # measured -- see VERSIONS.md/overlay.py timing notes), then slice each
        # field out of that single in-memory image rather than four separate
        # mss.grab() calls.
        left, top, right, bottom = self._client_rect_on_screen()
        client_size = (right - left, bottom - top)
        self.client_size = client_size

        # mss grabs the screen *region* where the panel sits, not the game's
        # own pixels, so anything on top of it is what would reach OCR. Refuse
        # the frame instead of reading someone else's window (~0.03ms measured
        # for the whole check, against ~60ms of OCR).
        points = [(left + x, top + y) for x, y in field_sample_points(client_size)]
        if panel_is_obscured(points, self._hwnd, self._root_window_at):
            raise RuntimeError(PANEL_OBSCURED)

        pbox = panel_box(client_size)
        shot = self._mss.grab({
            "left": left + pbox.left,
            "top": top + pbox.top,
            "width": pbox.right - pbox.left,
            "height": pbox.bottom - pbox.top,
        })
        panel = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        fields = {}
        for name, field_box in field_boxes(client_size).items():
            local = (
                field_box.left - pbox.left, field_box.top - pbox.top,
                field_box.right - pbox.left, field_box.bottom - pbox.top,
            )
            fields[name] = panel.crop(local)
        return fields


def get_capture(sample_path: str | Path | None = None) -> WindowCapture:
    if sample_path is not None:
        return StaticImageCapture(sample_path)
    if sys.platform == "win32":
        return GameWindowCapture()
    raise RuntimeError(
        "Real game capture requires Windows. Pass sample_path= to use "
        "StaticImageCapture for dev/testing on this platform."
    )
