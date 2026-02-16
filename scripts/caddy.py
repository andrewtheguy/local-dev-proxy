#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_dev_proxy.services import run_caddy_foreground


if __name__ == "__main__":
    raise SystemExit(run_caddy_foreground())
