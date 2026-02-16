from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterator

from filelock import FileLock

from .config import ProjectPaths


@contextmanager
def locked_active_services(paths: ProjectPaths) -> Iterator[set[str]]:
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(paths.state_lock_file))

    with lock:
        active = _read_state(paths.state_file)
        try:
            yield active
        except Exception:
            raise
        else:
            _write_state(paths.state_file, active)


def read_active_services(paths: ProjectPaths) -> set[str]:
    return _read_state(paths.state_file)


def _read_state(path: Path) -> set[str]:
    if not path.exists():
        return set()

    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return set()

    raw_services = payload.get("active_services", [])
    if not isinstance(raw_services, list):
        return set()

    return {str(item) for item in raw_services if isinstance(item, str)}


def _write_state(path: Path, active_services: set[str]) -> None:
    payload = {
        "active_services": sorted(active_services),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
