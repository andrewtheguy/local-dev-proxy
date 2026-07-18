from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from local_dev_proxy import config as config_module
from local_dev_proxy.config import ProjectPaths
from local_dev_proxy.gui import ManagerController, _tail_file
from local_dev_proxy.routes import load_routes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIGTEST_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "configtest.toml"
SCREENSHOTS = PROJECT_ROOT / "tmp" / "e2e-screenshots"


@dataclass
class _FakeService:
    name: str
    managed: bool
    disabled: bool
    status: str
    pid: int | None
    exit_code: int | None = None
    restart_count: int = 0


class FakeServiceManager:
    """Deterministic process-manager double backed by the requested TOML file."""

    def __init__(self, paths: ProjectPaths) -> None:
        manifest = load_routes(paths.services_file)
        self._logs_dir = paths.logs_dir
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._services: dict[str, _FakeService] = {}
        for offset, service in enumerate(manifest.services.values()):
            managed = not service.disabled and service.command is not None
            if service.disabled:
                status = "disabled"
            elif service.command is None:
                status = "unmanaged"
            else:
                status = "running"
            self._services[service.name] = _FakeService(
                name=service.name,
                managed=managed,
                disabled=service.disabled,
                status=status,
                pid=42000 + offset if managed else None,
            )
            log = self.get_log_path(service.name)
            log.write_text(
                "".join(
                    f"{service.name}: deterministic log line {line:03d}\n"
                    for line in range(350)
                )
            )

    def start_all(self) -> None:
        for offset, service in enumerate(self._services.values()):
            if service.managed:
                service.status = "running"
                service.pid = 42000 + offset
                service.exit_code = None

    def stop_all(self) -> None:
        for service in self._services.values():
            if service.managed:
                service.status = "stopped"
                service.pid = None
                service.exit_code = 0

    def _managed(self, name: str) -> _FakeService:
        service = self._services[name]
        if not service.managed:
            raise KeyError(f"Service '{name}' is unmanaged")
        return service

    def start_service(self, name: str) -> None:
        service = self._managed(name)
        service.status = "running"
        service.pid = 43000
        service.exit_code = None

    def stop_service(self, name: str) -> None:
        service = self._managed(name)
        service.status = "stopped"
        service.pid = None
        service.exit_code = 0

    def restart_service(self, name: str) -> None:
        service = self._managed(name)
        service.restart_count += 1
        service.status = "running"
        service.pid = 44000
        service.exit_code = None

    def get_status(self) -> list[dict[str, object]]:
        return [
            {
                "name": service.name,
                "status": service.status,
                "managed": service.managed,
                "pid": service.pid,
                "exit_code": service.exit_code,
                "restart_count": service.restart_count,
            }
            for service in self._services.values()
        ]

    def get_log_path(self, name: str) -> Path:
        if name not in self._services:
            raise KeyError(name)
        return self._logs_dir / f"{name}.log"

    def service_names(self) -> list[str]:
        return list(self._services)


class FakeProxy:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def isolated_configtest(tmp_path: Path) -> Iterator[tuple[Path, bytes]]:
    configtest = tmp_path / "configtest.toml"
    shutil.copyfile(CONFIGTEST_FIXTURE, configtest)
    original = configtest.read_bytes()
    try:
        yield configtest, original
    finally:
        configtest.write_bytes(original)
        configtest.with_name(f"{configtest.name}.tmp").unlink(missing_ok=True)


def _find_service_row(controller: ManagerController, name: str) -> int:
    model = controller.window.service_model
    for row in range(model.rowCount()):
        if model.index(row, 0).data() == name:
            return row
    raise AssertionError(f"Service row not found: {name}")


def _find_first_url(model: object, parent: QModelIndex = QModelIndex()) -> QModelIndex:
    for row in range(model.rowCount(parent)):
        url_index = model.index(row, 1, parent)
        if str(url_index.data()).startswith("http://"):
            return url_index
        child = _find_first_url(model, model.index(row, 0, parent))
        if child.isValid():
            return child
    return QModelIndex()


def test_tail_file_reads_only_requested_lines(tmp_path: Path) -> None:
    log = tmp_path / "service.log"
    log.write_text("one\ntwo\nthree\nfour\n")
    assert _tail_file(log, 2) == "three\nfour\n"
    assert _tail_file(log, 0) == ""
    assert _tail_file(tmp_path / "missing.log", 20) == ""


def test_macos_tray_icon_is_white_with_identical_alpha_mask() -> None:
    original = QImage(str(PROJECT_ROOT / "src/local_dev_proxy/assets/tray-icon.png"))
    macos = QImage(str(PROJECT_ROOT / "src/local_dev_proxy/assets/tray-icon-macos.png"))
    assert not original.isNull()
    assert not macos.isNull()
    assert macos.size() == original.size()

    opaque_pixels = 0
    for y in range(original.height()):
        for x in range(original.width()):
            original_color = original.pixelColor(x, y)
            macos_color = macos.pixelColor(x, y)
            assert macos_color.alpha() == original_color.alpha()
            if macos_color.alpha():
                opaque_pixels += 1
                assert macos_color.red() == 255
                assert macos_color.green() == 255
                assert macos_color.blue() == 255
    assert opaque_pixels == 546


def test_macos_selects_white_tray_icon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCAL_DEV_PROXY_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_module.sys, "platform", "darwin")
    selected = config_module.icon_path()
    expected = PROJECT_ROOT / "src/local_dev_proxy/assets/tray-icon-macos.png"
    assert selected is not None
    assert selected.name == "tray-icon-macos.png"
    assert selected.read_bytes() == expected.read_bytes()


def test_all_manager_flows_with_screenshots(
    qtbot: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_configtest: tuple[Path, bytes],
) -> None:
    configtest, original_config = isolated_configtest
    monkeypatch.setenv("LOCAL_DEV_PROXY_CONFIG_DIR", str(tmp_path / "icon-cache"))
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    opened_urls: list[str] = []
    managers: list[FakeServiceManager] = []
    proxies: list[FakeProxy] = []

    paths = ProjectPaths(
        config_dir=configtest.parent,
        services_file=configtest,
        logs_dir=tmp_path / "logs",
    )

    def service_factory(factory_paths: ProjectPaths) -> FakeServiceManager:
        manager = FakeServiceManager(factory_paths)
        managers.append(manager)
        return manager

    def proxy_factory(_factory_paths: ProjectPaths) -> FakeProxy:
        proxy = FakeProxy()
        proxies.append(proxy)
        return proxy

    controller = ManagerController(
        paths,
        lock=None,
        application=QApplication.instance(),
        service_factory=service_factory,
        proxy_factory=proxy_factory,
        url_opener=lambda url: opened_urls.append(url),
    )
    window = controller.window
    qtbot.addWidget(window)

    def screenshot(name: str) -> None:
        qtbot.wait(30)
        window.repaint()
        save_widget_screenshot(window, name)

    def save_widget_screenshot(widget: object, name: str) -> None:
        image = widget.grab()
        destination = SCREENSHOTS / f"{name}.png"
        assert not image.isNull()
        assert image.save(str(destination), "PNG")
        saved = QImage(str(destination))
        assert saved.width() > 0
        assert saved.height() > 0

    controller.start_services()
    controller.prime()
    window.show()
    qtbot.waitUntil(window.isVisible, timeout=2000)
    assert window.services_stack.currentWidget() is window.service_view
    assert window.service_model.rowCount() == 4
    screenshot("01-services-running")

    # Select and exercise every per-service lifecycle action.
    service_index = window.service_model.index(
        _find_service_row(controller, "s3browser"), 0
    )
    window.service_tree.scrollTo(service_index)
    QTest.mouseClick(
        window.service_tree.viewport(),
        Qt.MouseButton.LeftButton,
        pos=window.service_tree.visualRect(service_index).center(),
    )
    qtbot.waitUntil(window.stop_service_button.isEnabled)
    assert "s3browser (running)" in window.service_controls.title()
    screenshot("02-service-selected")

    qtbot.mouseClick(window.stop_service_button, Qt.MouseButton.LeftButton)
    assert (
        window.service_model.index(_find_service_row(controller, "s3browser"), 1).data()
        == "stopped"
    )
    assert window.start_service_button.isEnabled()
    screenshot("03-service-stopped")

    qtbot.mouseClick(window.start_service_button, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(window.restart_service_button, Qt.MouseButton.LeftButton)
    assert (
        window.service_model.index(_find_service_row(controller, "s3browser"), 3).data()
        == "1"
    )
    screenshot("04-service-restarted")

    # Read-only config, stop-to-edit, validation failure, reload, and save.
    qtbot.mouseClick(window.view_config_button, Qt.MouseButton.LeftButton)
    assert window.services_stack.currentWidget() is window.readonly_view
    assert window.readonly_config.toPlainText() == original_config.decode()
    assert window.edit_config_button.text() == "Stop All && Edit Config"
    assert window.edit_config_button.parentWidget() is window.services_tab
    back_position = window.view_config_button.mapTo(
        window, window.view_config_button.rect().topLeft()
    )
    edit_position = window.edit_config_button.mapTo(
        window, window.edit_config_button.rect().topLeft()
    )
    assert edit_position.x() > back_position.x()
    assert edit_position.y() == back_position.y()
    assert controller.running
    screenshot("05-config-readonly")

    qtbot.mouseClick(window.edit_config_button, Qt.MouseButton.LeftButton)
    assert window.services_stack.currentWidget() is window.editor_view
    assert not controller.running
    assert not window.tabs.isTabEnabled(window.tabs.indexOf(window.logs_tab))
    assert proxies[0].stopped
    assert window.start_all_button.isVisible()
    assert not window.view_config_button.isVisible()
    assert window.start_all_button.parentWidget() is window.services_tab
    assert (
        window.start_all_button.mapTo(
            window, window.start_all_button.rect().topLeft()
        ).y()
        < window.config_editor.mapTo(window, window.config_editor.rect().topLeft()).y()
    )
    screenshot("06-config-editing")

    window.config_editor.setPlainText(original_config.decode() + "\n[")
    qtbot.waitUntil(window.dirty_label.isVisible)
    qtbot.mouseClick(window.validate_button, Qt.MouseButton.LeftButton)
    assert window.status_label.text() == "invalid"
    assert window.services_banner.isVisible()
    screenshot("07-config-invalid")

    qtbot.mouseClick(window.reload_config_button, Qt.MouseButton.LeftButton)
    assert window.config_editor.toPlainText() == original_config.decode()
    assert not window.dirty_label.isVisible()
    screenshot("08-config-reloaded")

    saved_text = original_config.decode() + "\n# saved by the PySide6 E2E flow\n"
    window.config_editor.setPlainText(saved_text)
    qtbot.mouseClick(window.save_button, Qt.MouseButton.LeftButton)
    assert configtest.read_text() == saved_text
    assert window.status_label.text() == "saved ✓"
    assert not window.dirty_label.isVisible()
    screenshot("09-config-saved")

    qtbot.mouseClick(window.start_all_button, Qt.MouseButton.LeftButton)
    assert controller.running
    assert window.services_stack.currentWidget() is window.service_view
    assert window.tabs.isTabEnabled(window.tabs.indexOf(window.logs_tab))
    assert len(managers) == 2
    screenshot("10-services-started")

    # Logs: service selection, line limit, manual refresh, follow, and bottom pin.
    window.tabs.setCurrentWidget(window.logs_tab)
    window.log_service_combo.setCurrentText("s3browser")
    window.log_lines_spin.setValue(100)
    qtbot.mouseClick(window.refresh_logs_button, Qt.MouseButton.LeftButton)
    assert "s3browser: deterministic log line 349" in window.log_text.toPlainText()
    window.log_follow_check.setChecked(True)
    with managers[-1].get_log_path("s3browser").open("a") as log:
        log.write("s3browser: newest followed line\n")
    controller._refresh()
    qtbot.waitUntil(
        lambda: (
            window.log_text.verticalScrollBar().value()
            == window.log_text.verticalScrollBar().maximum()
        ),
        timeout=2000,
    )
    assert window.log_text.toPlainText().endswith("newest followed line\n")
    screenshot("11-logs-follow-bottom")

    # Real hierarchical route tree and URL activation.
    window.tabs.setCurrentWidget(window.routes_tab)
    assert window.route_model.rowCount() == 4
    s3browser_parent = window.route_model.index(1, 0)
    assert window.route_model.rowCount(s3browser_parent) == 1
    url_index = _find_first_url(window.route_model)
    assert url_index.isValid()
    window.route_tree.scrollTo(url_index)
    qtbot.wait(30)
    url_rect = window.route_tree.visualRect(url_index)
    assert not url_rect.isEmpty()
    QTest.mouseClick(
        window.route_tree.viewport(),
        Qt.MouseButton.LeftButton,
        pos=url_rect.center(),
    )
    qtbot.waitUntil(lambda: bool(opened_urls), timeout=2000)
    assert opened_urls[0].startswith("http://")
    screenshot("12-routes-tree")

    # The tray owns the requested Open/Quit menu, and its Open action restores a
    # window hidden through the normal close button.
    assert [
        action.text()
        for action in controller.tray_menu.actions()
        if not action.isSeparator()
    ] == [
        "Open Manager",
        "Quit",
    ]
    controller.tray_menu.popup(window.mapToGlobal(window.rect().center()))
    qtbot.waitUntil(controller.tray_menu.isVisible, timeout=2000)
    save_widget_screenshot(controller.tray_menu, "13-tray-menu")
    controller.tray_menu.hide()

    window.close()
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=2000)
    assert not controller._quitting
    controller.open_action.trigger()
    qtbot.waitUntil(window.isVisible, timeout=2000)
    screenshot("14-restored-from-tray")

    # In-window Quit follows the same cleanup path as the tray Quit action.
    qtbot.mouseClick(window.quit_button, Qt.MouseButton.LeftButton)
    assert controller._quitting
    assert not window.isVisible()
    assert proxies[-1].stopped


def test_tray_quit_action_uses_full_cleanup(
    qtbot: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_configtest: tuple[Path, bytes],
) -> None:
    configtest, original_config = isolated_configtest
    monkeypatch.setenv("LOCAL_DEV_PROXY_CONFIG_DIR", str(tmp_path / "icon-cache"))
    paths = ProjectPaths(configtest.parent, configtest, tmp_path / "logs")
    proxies: list[FakeProxy] = []

    def proxy_factory(_paths: ProjectPaths) -> FakeProxy:
        proxy = FakeProxy()
        proxies.append(proxy)
        return proxy

    controller = ManagerController(
        paths,
        lock=None,
        application=QApplication.instance(),
        service_factory=FakeServiceManager,
        proxy_factory=proxy_factory,
    )
    qtbot.addWidget(controller.window)
    controller.start_services()
    controller.prime()
    controller.window.show()

    controller.quit_action.trigger()

    assert controller._quitting
    assert not controller.window.isVisible()
    assert proxies[0].stopped
    assert configtest.read_bytes() == original_config
