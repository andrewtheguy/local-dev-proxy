from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
from typing import Mapping

from dotenv import dotenv_values


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    env_file: Path
    routes_file: Path
    data_dir: Path
    state_file: Path
    state_lock_file: Path
    layout_file: Path
    bootstrap_config_file: Path


def project_root() -> Path:
    override = os.environ.get("LOCAL_DEV_PROXY_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def get_paths(root: Path | None = None) -> ProjectPaths:
    root_path = (root or project_root()).resolve()
    data_dir = root_path / "data"
    return ProjectPaths(
        root=root_path,
        env_file=root_path / "config.env",
        routes_file=root_path / "routes.toml",
        data_dir=data_dir,
        state_file=data_dir / "active_services.json",
        state_lock_file=data_dir / ".active_services.lock",
        layout_file=root_path / "layouts" / "caddy.kdl",
        bootstrap_config_file=root_path / "config" / "caddy-bootstrap.json",
    )


def load_env(env_file: Path | None = None) -> dict[str, str]:
    source = env_file or get_paths().env_file
    loaded: dict[str, str] = {}

    if source.exists():
        for key, value in dotenv_values(source).items():
            if value is not None:
                loaded[key] = value

    # Process environment wins over file values.
    loaded.update({k: v for k, v in os.environ.items()})
    return loaded


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
