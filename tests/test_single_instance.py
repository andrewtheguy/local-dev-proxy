from __future__ import annotations

from pathlib import Path
import tomllib

import pytest

from local_dev_proxy import gui
from local_dev_proxy.config import (
    AlreadyRunningError,
    ProjectPaths,
    acquire_instance_lock,
    get_paths,
    release_instance_lock,
)
from local_dev_proxy.single_instance import (
    ActivationServer,
    activate_running_instance,
    instance_server_name,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_instance_lock_is_scoped_to_the_config_profile(tmp_path: Path) -> None:
    first_paths = get_paths(tmp_path / "first-profile")
    second_paths = get_paths(tmp_path / "second-profile")
    alternate_file_paths = ProjectPaths(
        config_dir=first_paths.config_dir,
        services_file=first_paths.config_dir / "alternate.toml",
        logs_dir=first_paths.logs_dir,
    )
    first_paths.config_dir.mkdir(parents=True)
    second_paths.config_dir.mkdir(parents=True)

    first = acquire_instance_lock(first_paths)
    second = acquire_instance_lock(second_paths)
    alternate = acquire_instance_lock(alternate_file_paths)
    try:
        with pytest.raises(AlreadyRunningError, match="already running"):
            acquire_instance_lock(first_paths)
        with pytest.raises(AlreadyRunningError, match="already running"):
            acquire_instance_lock(second_paths)
    finally:
        release_instance_lock(alternate)
        release_instance_lock(second)
        release_instance_lock(first)

    released_first = acquire_instance_lock(first_paths)
    released_second = acquire_instance_lock(second_paths)
    release_instance_lock(released_second)
    release_instance_lock(released_first)


def test_instance_server_name_is_stable_and_profile_specific(tmp_path: Path) -> None:
    first = get_paths(tmp_path / "first-profile")
    same = get_paths(tmp_path / "first-profile")
    second = get_paths(tmp_path / "second-profile")

    assert instance_server_name(first) == instance_server_name(same)
    assert instance_server_name(first) != instance_server_name(second)


def test_second_launch_activates_primary_window(
    qtbot: object,
    tmp_path: Path,
) -> None:
    paths = get_paths(tmp_path / "activation-profile")
    server = ActivationServer(paths)
    activations: list[bool] = []
    server.activated.connect(lambda: activations.append(True))
    try:
        assert activate_running_instance(paths, timeout_ms=100)
        qtbot.waitUntil(lambda: activations == [True], timeout=1000)
    finally:
        server.close()


def test_run_gui_activates_existing_instance_without_starting_another(
    qtbot: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = get_paths(tmp_path / "existing-profile")
    paths.config_dir.mkdir(parents=True)
    activated: list[object] = []

    def already_running(_paths: object) -> object:
        raise AlreadyRunningError("already running")

    monkeypatch.setattr(gui, "ensure_config", lambda _paths: paths)
    monkeypatch.setattr(gui, "dock_icon_path", lambda _config_dir: None)
    monkeypatch.setattr(gui, "acquire_instance_lock", already_running)
    monkeypatch.setattr(
        gui,
        "activate_running_instance",
        lambda received: activated.append(received) or True,
    )

    assert qtbot is not None
    assert gui.run_gui(paths) == 0
    assert activated == [paths]


def test_package_exposes_only_a_direct_gui_entry_point() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"]

    assert "scripts" not in project
    assert project["gui-scripts"] == {
        "local-dev-proxy": "local_dev_proxy.gui:run_gui"
    }
    assert all(not dependency.startswith("typer") for dependency in project["dependencies"])
    assert not (PROJECT_ROOT / "src/local_dev_proxy/cli.py").exists()
