"""Make the suite runnable as plain `pytest` from the repository root.

The package lives under `src/`, and some tests import helpers that live beside
them in `tests/` (notably `fixture_server`). Putting both on `sys.path` here
means `pytest tests/` works with no environment setup, on any platform, while
the documented `PYTHONPATH=src pytest` invocation keeps working unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for path in (ROOT / "src", ROOT / "tests"):
    entry = str(path)
    if entry not in sys.path:
        sys.path.insert(0, entry)
