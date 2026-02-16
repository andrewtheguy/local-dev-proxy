#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_dev_proxy.services import ServiceError, run_session_up


if __name__ == "__main__":
    try:
        raise SystemExit(run_session_up())
    except ServiceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
