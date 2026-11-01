"""Thin wrapper around RapidOCR (PP-OCR recognition, ONNX/CPU).

See VERSIONS.md for why rapidocr-onnxruntime stands in for the spec's originally
named PP-OCRv6-tiny ONNX model -- same OCR family, bundled models, no manual
model-file wiring.

Recognition-only, not detection+recognition: benchmarked live against the real
game (2026-08-17), detection (finding text regions in an image) was ~600-680ms
per call -- the entire OCR bottleneck the rest of the pipeline was tuned around.
Recognition alone (reading a pre-cropped, known-to-contain-one-line-of-text
image) was ~15ms. Since regions.py's field_boxes() already pins down exactly
where each field's text is, running detection to *re-discover* that on every
tick was pure waste -- see capture.py's grab_fields() for the cropping side of
this change.
"""
from __future__ import annotations

import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR


# The game HUD font is tiny (~14px tall) and the fixed-size field crops land at
# roughly 100x24 px. RapidOCR's recognition head is trained for larger text and
# drops thin glyphs -- notably EXP's '[' bracket, which the parser treats as
# structure and refuses a number without. Converting to greyscale (drops the
# green value-text tint and the coloured bar bleeding in at the crop edges) and
# a 3x LANCZOS upscale before recognition recovers those and measurably improves
# digit accuracy, for ~7ms/field. Verified on the new-UI 1366x768 sample.
_UPSCALE = 3

# The LV badge is white digits on a *bright orange* tile -- unlike every other
# field (light text on dark). In plain greyscale the white-on-orange contrast is
# weak enough that RapidOCR reads the thin '1' tile of e.g. '51' as '0'. A hard
# luminance threshold turns the digits pure white on black and fixes it. Only LV
# gets this: the same threshold eats HP/MP/EXP's '[' brackets and '/' slashes.
_LV_BINARIZE_THRESHOLD = 170


class StatPanelOcr:
    def __init__(self) -> None:
        self._engine = RapidOCR()

    def read_field(self, image: Image.Image, field: str | None = None) -> str:
        """Recognition-only OCR on a small pre-cropped single-line field crop.

        `field` ('LV'/'HP'/'MP'/'EXP') selects per-field preprocessing; omit it
        for the default greyscale path.
        """
        grey = image.convert("L")
        if field == "LV":
            grey = grey.point(lambda p: 255 if p > _LV_BINARIZE_THRESHOLD else 0)
        image = grey.convert("RGB")  # keep 3 channels for RapidOCR
        if _UPSCALE != 1:
            image = image.resize(
                (image.width * _UPSCALE, image.height * _UPSCALE), Image.LANCZOS
            )
        result, _elapse = self._engine(np.array(image), use_det=False, use_cls=False)
        if not result:
            return ""
        return result[0][0]
