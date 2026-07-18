from __future__ import annotations

import os

# Default the Qt tests to the headless "offscreen" platform so the suite runs
# without a display and no windows flash on screen. Set before PySide6 is
# imported (conftest loads before test modules). Override with an explicit
# QT_QPA_PLATFORM=cocoa (or similar) to watch the GUI locally.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
