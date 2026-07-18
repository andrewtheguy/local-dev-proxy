from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication

from local_dev_proxy import config
from local_dev_proxy.config import ProjectPaths
from local_dev_proxy.routes import load_routes


def test_environment_override_selects_an_isolated_config_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = tmp_path / "isolated-profile"
    monkeypatch.setenv("LOCAL_DEV_PROXY_CONFIG_DIR", str(override))

    paths = config.get_paths()

    assert paths == ProjectPaths(override)
    assert paths.services_file == override / "services.toml"
    assert paths.logs_dir == override / "logs"
    assert paths.instance_lock == override / ".instance.lock"


def test_default_config_uses_qt_app_config_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform_location = tmp_path / "platform-config"
    monkeypatch.delenv("LOCAL_DEV_PROXY_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        config.QStandardPaths,
        "writableLocation",
        lambda _location: str(platform_location),
    )

    assert config.user_config_dir() == platform_location
    assert QCoreApplication.applicationName() == "local-dev-proxy"
    assert QCoreApplication.organizationName() == "andrewtheguy"
    assert QCoreApplication.organizationDomain() == "andrewtheguy.com"


def test_one_profile_root_determines_every_application_path(tmp_path: Path) -> None:
    root = tmp_path / "profile"
    paths = ProjectPaths(root)

    resolved = config.ensure_config(paths)

    assert resolved is paths
    assert paths.root == root
    assert paths.services_file == root / "services.toml"
    assert paths.logs_dir == root / "logs"
    assert paths.instance_lock == root / ".instance.lock"
    assert paths.services_file.is_file()
    assert paths.logs_dir.is_dir()
    assert load_routes(paths.services_file).http_port == 2800


def test_ensure_config_does_not_replace_an_existing_profile(tmp_path: Path) -> None:
    paths = config.get_paths(tmp_path / "existing-profile")
    paths.root.mkdir(parents=True)
    existing = "http_port = 1234\nbind = [\"127.0.0.1\"]\n"
    paths.services_file.write_text(existing)

    config.ensure_config(paths)

    assert paths.services_file.read_text() == existing


def test_icon_cache_uses_the_injected_profile_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.sys, "platform", "darwin")

    paths = ProjectPaths(tmp_path)
    selected = config.icon_path(paths)

    assert selected == tmp_path / "tray-icon-macos.png"
    assert selected.is_file()
