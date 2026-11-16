#!/usr/bin/env python
"""Real entrypoint: run against the actual MapleStory window. Windows only --
requires the game running and pywin32 installed (see VERSIONS.md). Run from
repo root:

    .venv\\Scripts\\python scripts\\run_overlay.py

Add `--save-crops DIR` to write the field image behind every rejected reading
into DIR (plus an index.txt of what OCR made of each) -- the raw material for
tuning ocr.py. Works on the built .exe too.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from maple_analyzer.capture import GameWindowCapture
from maple_analyzer.overlay import OverlayApp

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MapleStoryAnalyzer HUD")
    parser.add_argument("--save-crops", metavar="DIR", default=None,
                        help="save the field images behind rejected readings into DIR")
    args = parser.parse_args()
    app = OverlayApp(GameWindowCapture(), save_crops_dir=args.save_crops)
    app.run()
