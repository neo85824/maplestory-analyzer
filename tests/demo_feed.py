"""Test helper: a fake "camera" whose EXP value changes every frame.

There is no game window on the dev box, and the one real screenshot never
changes. This redraws the EXP text in that screenshot with an incrementing
value, at the EXP field's real pixel box, so test_demo_feed.py can run real
OCR and the real parser over several *different* frames in a row. Only the
image is fabricated; nothing downstream is mocked.
"""
from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from maple_analyzer.regions import field_boxes


class DemoExpFeed:
    def __init__(self, sample_path: str | Path, start_exp: int = 162950, exp_per_tick: tuple[int, int] = (40, 220)):
        self._base = Image.open(sample_path).convert("RGB")
        self._exp = start_exp
        self._exp_per_tick = exp_per_tick
        # A crisp ~14px face so the redrawn EXP text (incl. its '[' bracket, which
        # the parser treats as structure) survives OCR. Prefer a real TrueType
        # bold if the dev box has one; otherwise Pillow's sized bitmap default,
        # which recognises reliably at this size.
        self._font = None
        for name in (
            "DejaVuSansMono-Bold.ttf", "DejaVuSans-Bold.ttf",
            "/usr/share/fonts/gnu-free/FreeMonoBold.otf",
            "/usr/share/fonts/Adwaita/AdwaitaMono-Bold.ttf",
            "consolab.ttf", "cour.ttf",
        ):
            try:
                self._font = ImageFont.truetype(name, 13)
                break
            except OSError:
                continue
        if self._font is None:
            self._font = ImageFont.load_default(size=14)

    def _render_frame(self) -> Image.Image:
        frame = self._base.copy()
        draw = ImageDraw.Draw(frame)
        exp = field_boxes(self._base.size)["EXP"]
        l, t, b = exp.left, exp.top, exp.bottom
        bg = frame.getpixel((l - 3, (t + b) // 2))  # background just left of the field
        draw.rectangle((l, t - 2, exp.right + 40, b + 2), fill=bg)
        pct = (self._exp % 10000) / 100  # synthetic, just needs to move plausibly
        draw.text((l, t - 2), f"EXP {self._exp}[{pct:05.2f}%]", font=self._font, fill=(230, 255, 120))
        return frame

    def grab_fields(self) -> dict[str, Image.Image]:
        self._exp += random.randint(*self._exp_per_tick)
        frame = self._render_frame()
        return {name: frame.crop(box.as_tuple()) for name, box in field_boxes(self._base.size).items()}
