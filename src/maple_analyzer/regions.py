"""Crop-box definitions for the stat panel and its fields.

The MapleStory "classic" client's bottom HUD (new UI, 2026-09) is a fixed
*pixel* height bar anchored to the bottom of the client area. The LV block is
pinned to the left edge; the HP/MP/EXP values sit at a fixed pixel offset from
the right edge, just left of the shop-button cluster (which is itself
right-pinned). Nothing in this bar scales with the window -- the game only
offers a fixed list of client resolutions, and the bar looks pixel-identical
across all of them.

So each field box is defined as fixed pixel insets from a client-area edge:
X from the left edge for LV, from the right edge for HP/MP/EXP; Y always
measured up from the bottom edge. `field_boxes()` resolves the spec against a
concrete client size.

(This replaced an earlier linear-scaling model keyed to a 1351x800 reference
screenshot -- wrong for a fixed-size, edge-anchored bar, and broken outright by
the new UI's layout change.)

A more robust future approach: detect the coloured HP/MP/EXP bars (pure
red/blue/yellow-green rects, fixed size, directly under each value) and derive
the text boxes from them -- fully resolution/layout independent. Not done yet:
needs contiguity+size filtering to reject the orange LV badge and the magenta
shop button.
"""
from __future__ import annotations

from dataclasses import dataclass

# Measured off samples/maple_story_new_ui_1366.png (client 1366x768), then
# OCR-verified field-by-field through the real pipeline.
#   (x_anchor, x_near, x_far, y_top_up, y_bottom_up)
# x_anchor "left":  left = x_near,      right = x_far
# x_anchor "right": left = W - x_near,  right = W - x_far
# y (both): top = H - y_top_up,  bottom = H - y_bottom_up
# The HP/MP/EXP value text needs one px of headroom top and bottom past its ink
# -- a tighter box clips glyph tops/tails just enough that RapidOCR flips digits
# (7->2, 9->3). 26px tall (35..9 up from the bottom) is the widest that still
# excludes the coloured bar directly below; going wider re-introduces misreads
# from the bar bleeding in.
_FIELD_SPECS: dict[str, tuple[str, int, int, int, int]] = {
    "LV": ("left", 8, 92, 36, 6),
    "HP": ("right", 902, 804, 35, 9),
    "MP": ("right", 738, 640, 35, 9),
    "EXP": ("right", 572, 436, 35, 9),
}


@dataclass(frozen=True)
class Box:
    left: int
    top: int
    right: int
    bottom: int

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


def _resolve(spec: tuple[str, int, int, int, int], client_size: tuple[int, int]) -> Box:
    anchor, x_near, x_far, y_top_up, y_bottom_up = spec
    cw, ch = client_size
    if anchor == "left":
        left, right = x_near, x_far
    else:
        left, right = cw - x_near, cw - x_far
    return Box(left, ch - y_top_up, right, ch - y_bottom_up)


def field_boxes(client_size: tuple[int, int]) -> dict[str, Box]:
    """Absolute per-field crop boxes ('LV'/'HP'/'MP'/'EXP') for the given
    client size -- see ocr.py's read_field() for what consumes them."""
    return {name: _resolve(spec, client_size) for name, spec in _FIELD_SPECS.items()}


def panel_box(client_size: tuple[int, int]) -> Box:
    """Bounding box over every field box: grabbed once per tick, then the field
    boxes are sliced out of it in-memory (no extra screen captures)."""
    boxes = field_boxes(client_size).values()
    return Box(
        min(b.left for b in boxes),
        min(b.top for b in boxes),
        max(b.right for b in boxes),
        max(b.bottom for b in boxes),
    )
