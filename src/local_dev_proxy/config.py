from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
from typing import Mapping


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    services_file: Path
    layout_file: Path


def project_root() -> Path:
    override = os.environ.get("LOCAL_DEV_PROXY_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def get_paths(root: Path | None = None) -> ProjectPaths:
    root_path = (root or project_root()).resolve()
    return ProjectPaths(
        root=root_path,
        services_file=root_path / "services.toml",
        layout_file=root_path / "layouts" / "caddy.kdl",
    )


def require_port(env: Mapping[str, str], key: str) -> int:
    raw = env.get(key)
    if raw is None:
        raise ValueError(f"Missing required port variable: {key}")

    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got: {raw!r}") from exc

    if value < 1 or value > 65535:
        raise ValueError(f"{key} must be in range 1-65535, got: {value}")

    return value
