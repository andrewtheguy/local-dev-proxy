from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_dev_proxy.config import ProjectPaths
from local_dev_proxy.state import locked_active_services, read_active_services


def _paths(tmp_path: Path) -> ProjectPaths:
    data_dir = tmp_path / "data"
    return ProjectPaths(
        root=tmp_path,
        env_file=tmp_path / "config.env",
        routes_file=tmp_path / "routes.toml",
        data_dir=data_dir,
        state_file=data_dir / "active_services.json",
        state_lock_file=data_dir / ".active_services.lock",
        caddy_pid_file=data_dir / "caddy.pid",
        layout_file=tmp_path / "layouts" / "caddy.kdl",
        bootstrap_config_file=tmp_path / "config" / "caddy-bootstrap.json",
    )


def test_locked_active_services_persists_updates(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    with locked_active_services(paths) as active:
        active.add("s3browser")

    assert read_active_services(paths) == {"s3browser"}

    payload = json.loads(paths.state_file.read_text())
    assert payload["active_services"] == ["s3browser"]
    assert isinstance(payload["updated_at"], str)


def test_locked_active_services_rolls_back_on_error(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    with pytest.raises(RuntimeError, match="boom"):
        with locked_active_services(paths) as active:
            active.add("minio")
            raise RuntimeError("boom")

    assert read_active_services(paths) == set()
    assert not paths.state_file.exists()
