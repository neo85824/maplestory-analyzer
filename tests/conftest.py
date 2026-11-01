import sys
from pathlib import Path

# Same src-on-path convention as scripts/run_overlay*.py -- this repo has no
# packaging config (no pyproject/setup.py), so tests need the same manual
# sys.path insert to import maple_analyzer.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# tests/ is not a package (no __init__.py), so a sibling fixture module like
# captured_frames.py isn't importable by default -- add tests/ itself too.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Real client screenshot, new UI (2026-09), client 1366x768. DPI-aware GDI
# grab of the live window -- see handover-2026-09-10.md.
SAMPLE_IMAGE = Path(__file__).resolve().parent.parent / "samples" / "maple_story_new_ui_1366.png"
