"""PySide6 manager UI for the local development proxy.

The Qt event loop owns one :class:`ManagerWindow` and one system-tray icon. The
``ManagerController`` keeps all process, proxy, configuration, log, and route
behavior outside the widget construction so the same flows can be exercised in
GUI tests with a deterministic runtime.
"""

# Widget attributes are initialized synchronously by the three _build_* helpers
# called from __init__; basedpyright does not follow those calls for this rule.
# pyright: reportUninitializedInstanceVariable=false

from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from filelock import FileLock
from PySide6.QtCore import (
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    QSignalBlocker,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .config import (
    AlreadyRunningError,
    ProjectPaths,
    acquire_instance_lock,
    dock_icon_path,
    ensure_config,
    icon_path,
    release_instance_lock,
)
from .routes import (
    RouteConfigError,
    ServiceDef,
    ServiceRoute,
    load_routes,
    validate_toml,
)
from .services import start_proxy, start_services_managed

logger = logging.getLogger(__name__)

_POLL_MS = 2000
_SIGNAL_POLL_MS = 300
_CONTROLLABLE = frozenset({"running", "stopped", "crashed"})
_NEUTRAL, _SUCCESS, _ERROR = 0, 1, 2
_STATUS_COLORS = {
    _NEUTRAL: "#30343b",
    _SUCCESS: "#08752f",
    _ERROR: "#b42318",
}


class ServiceManagerLike(Protocol):
    def start_all(self) -> None: ...
    def stop_all(self) -> None: ...
    def start_service(self, name: str) -> None: ...
    def stop_service(self, name: str) -> None: ...
    def restart_service(self, name: str) -> None: ...
    def get_status(self) -> list[dict[str, object]]: ...
    def get_log_path(self, name: str) -> Path: ...
    def service_names(self) -> list[str]: ...


class ProxyLike(Protocol):
    def stop(self) -> None: ...


ServiceFactory = Callable[[ProjectPaths], ServiceManagerLike]
ProxyFactory = Callable[[ProjectPaths], ProxyLike]
UrlOpener = Callable[[str], object]


def _acquire_lock() -> FileLock:
    """Take the single-instance lock; exit if another manager holds it."""
    try:
        return acquire_instance_lock()
    except AlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


def _tail_file(path: Path, lines: int) -> str:
    """Read the last ``lines`` lines without loading an entire large log."""
    if not path.exists() or lines <= 0:
        return ""
    chunk = 8192
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            buffer = b""
            while position > 0 and buffer.count(b"\n") <= lines:
                read_size = min(chunk, position)
                position -= read_size
                handle.seek(position)
                buffer = handle.read(read_size) + buffer
    except OSError:
        return ""
    data = buffer.decode(errors="replace").splitlines()
    tail = data[-lines:] if len(data) > lines else data
    return "\n".join(tail) + ("\n" if tail else "")


def _icon(path: Path | None) -> QIcon:
    if path is None:
        return QIcon()
    icon = QIcon(str(path))
    if icon.isNull():
        logger.warning("Could not load icon from %s", path)
    return icon


def _monospace_font(point_size: int | None = None) -> QFont:
    font = QFont(QApplication.font())
    font.setFamily("Menlo")
    font.setStyleHint(QFont.StyleHint.Monospace)
    if point_size is not None:
        font.setPointSize(point_size)
    return font


def _read_only_item(text: object) -> QStandardItem:
    item = QStandardItem(str(text))
    item.setEditable(False)
    return item


class ManagerWindow(QMainWindow):
    """Concrete Qt Widgets view with stable object names for GUI automation."""

    quit_requested = Signal()

    services_tab: QWidget
    services_banner: QLabel
    view_config_button: QPushButton
    status_label: QLabel
    services_stack: QStackedWidget
    service_view: QWidget
    service_model: QStandardItemModel
    service_tree: QTreeView
    service_controls: QGroupBox
    start_service_button: QPushButton
    stop_service_button: QPushButton
    restart_service_button: QPushButton
    readonly_view: QWidget
    readonly_config: QPlainTextEdit
    edit_config_button: QPushButton
    editor_view: QWidget
    config_editor: QPlainTextEdit
    start_all_button: QPushButton
    validate_button: QPushButton
    save_button: QPushButton
    reload_config_button: QPushButton
    dirty_label: QLabel
    logs_tab: QWidget
    log_service_combo: QComboBox
    log_lines_spin: QSpinBox
    log_follow_check: QCheckBox
    refresh_logs_button: QPushButton
    log_text: QPlainTextEdit
    routes_tab: QWidget
    routes_banner: QLabel
    route_model: QStandardItemModel
    route_tree: QTreeView
    reload_routes_button: QPushButton

    def __init__(self, app_icon: QIcon) -> None:
        super().__init__()
        self._allow_close = False
        self.setObjectName("manager_window")
        self.setWindowTitle(f"Local Dev Proxy — Manager v{__version__}")
        self.setWindowIcon(app_icon)
        self.resize(960, 680)
        self.setMinimumSize(720, 500)
        self._build_ui()
        self._apply_style()

    def _build_ui(self) -> None:
        central = QWidget(self)
        central.setObjectName("central")
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)

        toolbar = QFrame(central)
        toolbar.setObjectName("toolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(2, 0, 2, 0)
        self.version_label = QLabel(f"v{__version__}", toolbar)
        self.version_label.setObjectName("version_label")
        toolbar_layout.addWidget(self.version_label)
        toolbar_layout.addStretch(1)
        self.quit_button = QPushButton("Quit", toolbar)
        self.quit_button.setObjectName("quit_button")
        toolbar_layout.addWidget(self.quit_button)
        root.addWidget(toolbar)

        self.tabs = QTabWidget(central)
        self.tabs.setObjectName("main_tabs")
        self.tabs.setDocumentMode(True)
        root.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self._build_services_tab()
        self._build_logs_tab()
        self._build_routes_tab()
        self.quit_button.clicked.connect(self.quit_requested)

        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.quit_requested)
        self.addAction(quit_action)

    def _build_services_tab(self) -> None:
        self.services_tab = QWidget(self.tabs)
        self.services_tab.setObjectName("services_tab")
        layout = QVBoxLayout(self.services_tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.services_banner = QLabel(self.services_tab)
        self.services_banner.setObjectName("services_banner")
        self.services_banner.setWordWrap(True)
        self.services_banner.hide()
        layout.addWidget(self.services_banner)

        lifecycle = QHBoxLayout()
        self.view_config_button = QPushButton("View Config", self.services_tab)
        self.view_config_button.setObjectName("view_config_button")
        lifecycle.addWidget(self.view_config_button)
        # A single ampersand is a hidden Qt mnemonic marker. Doubling it paints
        # the requested literal "&" in the button label.
        self.edit_config_button = QPushButton(
            "Stop All && Edit Config", self.services_tab
        )
        self.edit_config_button.setObjectName("edit_config_button")
        lifecycle.addWidget(self.edit_config_button)
        self.start_all_button = QPushButton("Start All", self.services_tab)
        self.start_all_button.setObjectName("start_all_button")
        lifecycle.addWidget(self.start_all_button)
        lifecycle.addStretch(1)
        self.status_label = QLabel(self.services_tab)
        self.status_label.setObjectName("status_label")
        lifecycle.addWidget(self.status_label)
        layout.addLayout(lifecycle)

        self.services_stack = QStackedWidget(self.services_tab)
        self.services_stack.setObjectName("services_stack")
        layout.addWidget(self.services_stack, 1)

        self.service_view = QWidget(self.services_stack)
        service_layout = QVBoxLayout(self.service_view)
        service_layout.setContentsMargins(0, 0, 0, 0)
        service_layout.setSpacing(8)
        self.service_model = QStandardItemModel(0, 5, self)
        self.service_model.setHorizontalHeaderLabels(
            ["Service", "Status", "PID", "Restarts", "Exit code"]
        )
        self.service_tree = QTreeView(self.service_view)
        self.service_tree.setObjectName("service_tree")
        self.service_tree.setModel(self.service_model)
        self.service_tree.setRootIsDecorated(False)
        self.service_tree.setAlternatingRowColors(True)
        self.service_tree.setUniformRowHeights(True)
        self.service_tree.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.service_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.service_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.service_tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        for column in range(1, 5):
            self.service_tree.header().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        service_layout.addWidget(self.service_tree, 1)

        self.service_controls = QGroupBox(
            "Selected service — click a row to control it", self.service_view
        )
        self.service_controls.setObjectName("service_controls")
        controls_layout = QHBoxLayout(self.service_controls)
        self.start_service_button = QPushButton("Start", self.service_controls)
        self.start_service_button.setObjectName("start_service_button")
        self.stop_service_button = QPushButton("Stop", self.service_controls)
        self.stop_service_button.setObjectName("stop_service_button")
        self.restart_service_button = QPushButton("Restart", self.service_controls)
        self.restart_service_button.setObjectName("restart_service_button")
        controls_layout.addWidget(self.start_service_button)
        controls_layout.addWidget(self.stop_service_button)
        controls_layout.addWidget(self.restart_service_button)
        controls_layout.addStretch(1)
        service_layout.addWidget(self.service_controls)
        self.services_stack.addWidget(self.service_view)

        self.readonly_view = QWidget(self.services_stack)
        readonly_layout = QVBoxLayout(self.readonly_view)
        readonly_layout.setContentsMargins(0, 0, 0, 0)
        readonly_layout.setSpacing(8)
        self.readonly_config = QPlainTextEdit(self.readonly_view)
        self.readonly_config.setObjectName("readonly_config")
        self.readonly_config.setReadOnly(True)
        self.readonly_config.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.readonly_config.setFont(_monospace_font())
        readonly_layout.addWidget(self.readonly_config, 1)
        self.services_stack.addWidget(self.readonly_view)

        self.editor_view = QWidget(self.services_stack)
        editor_layout = QVBoxLayout(self.editor_view)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(8)
        self.config_editor = QPlainTextEdit(self.editor_view)
        self.config_editor.setObjectName("config_editor")
        self.config_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.config_editor.setFont(_monospace_font())
        editor_layout.addWidget(self.config_editor, 1)
        editor_actions = QHBoxLayout()
        self.validate_button = QPushButton("Validate", self.editor_view)
        self.validate_button.setObjectName("validate_button")
        self.save_button = QPushButton("Save", self.editor_view)
        self.save_button.setObjectName("save_button")
        self.reload_config_button = QPushButton("Reload from disk", self.editor_view)
        self.reload_config_button.setObjectName("reload_config_button")
        for button in (
            self.validate_button,
            self.save_button,
            self.reload_config_button,
        ):
            editor_actions.addWidget(button)
        editor_actions.addStretch(1)
        self.dirty_label = QLabel("● unsaved changes", self.editor_view)
        self.dirty_label.setObjectName("dirty_label")
        self.dirty_label.hide()
        editor_actions.addWidget(self.dirty_label)
        editor_layout.addLayout(editor_actions)
        self.services_stack.addWidget(self.editor_view)

        self.tabs.addTab(self.services_tab, "Services")

    def _build_logs_tab(self) -> None:
        self.logs_tab = QWidget(self.tabs)
        self.logs_tab.setObjectName("logs_tab")
        layout = QVBoxLayout(self.logs_tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Service:", self.logs_tab))
        self.log_service_combo = QComboBox(self.logs_tab)
        self.log_service_combo.setObjectName("log_service_combo")
        self.log_service_combo.setMinimumWidth(190)
        controls.addWidget(self.log_service_combo)
        controls.addWidget(QLabel("Lines:", self.logs_tab))
        self.log_lines_spin = QSpinBox(self.logs_tab)
        self.log_lines_spin.setObjectName("log_lines_spin")
        self.log_lines_spin.setRange(10, 5000)
        self.log_lines_spin.setSingleStep(10)
        self.log_lines_spin.setValue(200)
        controls.addWidget(self.log_lines_spin)
        self.log_follow_check = QCheckBox("Follow", self.logs_tab)
        self.log_follow_check.setObjectName("log_follow_check")
        controls.addWidget(self.log_follow_check)
        controls.addStretch(1)
        self.refresh_logs_button = QPushButton("Refresh", self.logs_tab)
        self.refresh_logs_button.setObjectName("refresh_logs_button")
        controls.addWidget(self.refresh_logs_button)
        layout.addLayout(controls)

        self.log_text = QPlainTextEdit(self.logs_tab)
        self.log_text.setObjectName("log_text")
        self.log_text.setReadOnly(True)
        self.log_text.setFont(_monospace_font(10))
        self.log_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(self.log_text, 1)
        self.tabs.addTab(self.logs_tab, "Logs")

    def _build_routes_tab(self) -> None:
        self.routes_tab = QWidget(self.tabs)
        self.routes_tab.setObjectName("routes_tab")
        layout = QVBoxLayout(self.routes_tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        hint = QLabel(
            "Routes are grouped by service. Click a URL row to open it.",
            self.routes_tab,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.routes_banner = QLabel(self.routes_tab)
        self.routes_banner.setObjectName("routes_banner")
        self.routes_banner.setWordWrap(True)
        self.routes_banner.hide()
        layout.addWidget(self.routes_banner)

        self.route_model = QStandardItemModel(0, 3, self)
        self.route_model.setHorizontalHeaderLabels(
            ["Service / route", "URL", "→ proxies to"]
        )
        self.route_tree = QTreeView(self.routes_tab)
        self.route_tree.setObjectName("route_tree")
        self.route_tree.setModel(self.route_model)
        self.route_tree.setAlternatingRowColors(True)
        self.route_tree.setUniformRowHeights(True)
        self.route_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.route_tree.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.route_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.route_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.route_tree.header().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        layout.addWidget(self.route_tree, 1)
        route_actions = QHBoxLayout()
        self.reload_routes_button = QPushButton("Reload", self.routes_tab)
        self.reload_routes_button.setObjectName("reload_routes_button")
        route_actions.addWidget(self.reload_routes_button)
        route_actions.addStretch(1)
        layout.addLayout(route_actions)
        self.tabs.addTab(self.routes_tab, "Routes")

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget#central { background: #f7f8fa; color: #1d2939; }
            QLabel#version_label { color: #667085; }
            QLabel#services_banner, QLabel#routes_banner {
                color: #b42318; background: #fef3f2; border: 1px solid #fecdca;
                border-radius: 4px; padding: 6px;
            }
            QLabel#dirty_label { color: #b54708; }
            QPushButton#start_all_button {
                color: #ffffff; background: #175cd3; border: 1px solid #1849a9;
                border-radius: 4px; font-weight: 600;
            }
            QPushButton#start_all_button:hover { background: #1849a9; }
            QPushButton#start_all_button:pressed { background: #194185; }
            QTabWidget::pane { border: 1px solid #cfd4dc; background: #ffffff; }
            QTabBar::tab { padding: 8px 18px; }
            QTreeView, QPlainTextEdit {
                background: #ffffff; border: 1px solid #98a2b3;
                selection-background-color: #cfe1f7; selection-color: #101828;
            }
            QPlainTextEdit[readOnly="true"] {
                background: #f2f4f7; color: #475467; border-color: #d0d5dd;
            }
            QHeaderView::section {
                background: #eaecf0; color: #344054; padding: 6px;
                border: 0; border-right: 1px solid #d0d5dd;
                border-bottom: 1px solid #98a2b3; font-weight: 600;
            }
            QPushButton { min-height: 24px; padding: 2px 10px; }
            QGroupBox { font-weight: 600; }
            """
        )

    def set_service_mode(self, mode: str) -> None:
        pages = {
            "services": self.service_view,
            "readonly": self.readonly_view,
            "edit": self.editor_view,
        }
        self.services_stack.setCurrentWidget(pages[mode])
        self.view_config_button.setVisible(mode != "edit")
        self.edit_config_button.setVisible(mode == "readonly")
        self.start_all_button.setVisible(mode == "edit")
        if mode == "services":
            self.view_config_button.setText("View Config")
        elif mode == "readonly":
            self.view_config_button.setText("Back to Services")
        editing = mode == "edit"
        if editing:
            self.tabs.setCurrentWidget(self.services_tab)
        self.tabs.setTabEnabled(self.tabs.indexOf(self.logs_tab), not editing)
        self.tabs.setTabEnabled(self.tabs.indexOf(self.routes_tab), not editing)

    def set_banner(self, text: str) -> None:
        self.services_banner.setText(text)
        self.services_banner.setVisible(bool(text))

    def set_routes_banner(self, text: str) -> None:
        self.routes_banner.setText(text)
        self.routes_banner.setVisible(bool(text))

    def set_status(self, text: str, level: int) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"color: {_STATUS_COLORS[level]}; font-weight: 600;"
        )

    def set_dirty(self, dirty: bool) -> None:
        self.dirty_label.setVisible(dirty)

    def show_and_raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def allow_close(self) -> None:
        self._allow_close = True

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self._allow_close:
            event.accept()
            return
        event.ignore()
        self.hide()


class ManagerController:
    """Own the proxy/service runtime and connect it to :class:`ManagerWindow`."""

    def __init__(
        self,
        paths: ProjectPaths,
        lock: FileLock | None,
        *,
        application: QApplication | None = None,
        service_factory: ServiceFactory = start_services_managed,
        proxy_factory: ProxyFactory = start_proxy,
        url_opener: UrlOpener = webbrowser.open,
    ) -> None:
        app = application or QApplication.instance()
        if not isinstance(app, QApplication):
            raise RuntimeError("Create QApplication before ManagerController")
        self.application = app
        self.paths = paths
        self._lock = lock
        self._service_factory = service_factory
        self._proxy_factory = proxy_factory
        self._url_opener = url_opener
        self.service_manager: ServiceManagerLike | None = None
        self.proxy: ProxyLike | None = None
        self.running = False
        self._quitting = False
        self._view = "services"
        self._mode_banner = ""
        self._last_running: bool | None = None
        self._selected_name: str | None = None
        self._display_names: list[str] = []
        self._status_by_name: dict[str, str] = {}
        self._log_names: list[str] = []
        self._loaded_config = ""
        self._shutdown_flag = threading.Event()

        self.window = ManagerWindow(_icon(dock_icon_path()))
        self.tray = QSystemTrayIcon(_icon(icon_path()), self.window)
        self.tray.setObjectName("system_tray")
        self.tray.setToolTip("Local Dev Proxy")
        self.tray_menu = QMenu()
        self.open_action = self.tray_menu.addAction("Open Manager")
        self.open_action.setObjectName("open_manager_action")
        self.tray_menu.addSeparator()
        self.quit_action = self.tray_menu.addAction("Quit")
        self.quit_action.setObjectName("quit_action")
        self.tray.setContextMenu(self.tray_menu)

        self._bind_callbacks()
        self._refresh_timer = QTimer(self.window)
        self._refresh_timer.setInterval(_POLL_MS)
        self._refresh_timer.timeout.connect(self._refresh)
        self._signal_timer = QTimer(self.window)
        self._signal_timer.setInterval(_SIGNAL_POLL_MS)
        self._signal_timer.timeout.connect(self._poll_signals)
        self._atexit_callback = self._stop_children_safely
        atexit.register(self._atexit_callback)

    def _bind_callbacks(self) -> None:
        window = self.window
        window.quit_requested.connect(self.quit)
        window.view_config_button.clicked.connect(self._toggle_readonly)
        window.edit_config_button.clicked.connect(self._stop_to_edit)
        window.start_all_button.clicked.connect(self._start_all)
        window.validate_button.clicked.connect(self._validate)
        window.save_button.clicked.connect(self._save)
        window.reload_config_button.clicked.connect(self._reload)
        window.start_service_button.clicked.connect(lambda: self._act("start_service"))
        window.stop_service_button.clicked.connect(lambda: self._act("stop_service"))
        window.restart_service_button.clicked.connect(
            lambda: self._act("restart_service")
        )
        window.service_tree.selectionModel().selectionChanged.connect(
            self._on_row_selected
        )
        window.config_editor.textChanged.connect(self._on_config_edited)
        window.log_service_combo.currentIndexChanged.connect(self._on_select_log)
        window.log_lines_spin.valueChanged.connect(lambda _value: self._refresh_logs())
        window.log_follow_check.toggled.connect(self._on_follow_toggled)
        window.refresh_logs_button.clicked.connect(self._refresh_logs)
        window.reload_routes_button.clicked.connect(self._reload_routes)
        window.route_tree.clicked.connect(self._open_route_index)
        self.open_action.triggered.connect(self._show_window)
        self.quit_action.triggered.connect(self.quit)
        self.tray.activated.connect(self._tray_activated)

    # --- in-process lifecycle -------------------------------------------------

    def start_services(self) -> None:
        if self.running:
            return
        try:
            self.service_manager = self._service_factory(self.paths)
            self.service_manager.start_all()
            self.proxy = self._proxy_factory(self.paths)
        except Exception:
            self.stop_services()
            raise
        self.running = True

    def stop_services(self) -> None:
        proxy, self.proxy = self.proxy, None
        try:
            if proxy is not None:
                proxy.stop()
        except Exception:
            logger.exception("Error stopping proxy during shutdown")
        finally:
            try:
                if self.service_manager is not None:
                    self.service_manager.stop_all()
            except Exception:
                logger.exception("Error stopping services during shutdown")
            finally:
                self.running = False

    def _stop_children_safely(self) -> None:
        try:
            self.stop_services()
        except Exception:  # noqa: BLE001 - exit-time best effort
            pass

    # --- lifecycle / event loop ----------------------------------------------

    def prime(self) -> None:
        self._populate_log_services()
        self._reload_routes()
        self._refresh()

    def start_timers(self) -> None:
        self._refresh_timer.start()
        self._signal_timer.start()

    def install_signals(self) -> None:
        for signame in ("SIGTERM", "SIGINT", "SIGHUP"):
            sig = getattr(signal, signame, None)
            if sig is not None:
                signal.signal(sig, lambda *_args: self._shutdown_flag.set())

    def _poll_signals(self) -> None:
        if self._shutdown_flag.is_set():
            self.quit()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self._show_window()

    def _show_window(self) -> None:
        self.window.show_and_raise()

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._refresh_timer.stop()
        self._signal_timer.stop()
        self.stop_services()
        self.tray.hide()
        if self._lock is not None:
            release_instance_lock(self._lock)
            self._lock = None
        atexit.unregister(self._atexit_callback)
        self.window.allow_close()
        self.window.close()
        self.application.quit()

    # --- view / refresh -------------------------------------------------------

    def _set_status(self, text: str, level: int = _NEUTRAL) -> None:
        self.window.set_status(text, level)

    def _set_banner(self, text: str) -> None:
        self.window.set_banner(text)

    def _set_view(self, view: str) -> None:
        self._view = view
        if view == "services":
            self._mode_banner = ""
            self._refresh_service_view()
        elif view == "readonly":
            self._load_file()
            self._mode_banner = (
                "Viewing configuration (read-only) — services are still running."
            )
        else:
            self._load_file()
            self._mode_banner = (
                "Editing configuration — Start All validates, saves, and launches it."
            )
        self.window.set_service_mode(view)
        self._set_banner(self._mode_banner)

    def _toggle_readonly(self) -> None:
        self._set_view("services" if self._view == "readonly" else "readonly")

    def _sync_run_state(self) -> None:
        if self.running != self._last_running:
            self._set_view("services" if self.running else "edit")
            self._last_running = self.running

    def _refresh(self) -> None:
        self._sync_run_state()
        if self._view == "services":
            self._refresh_service_view()
        if self.running and self.window.log_follow_check.isChecked():
            self._refresh_logs()

    def _refresh_service_view(self) -> None:
        self._sync_service_tree()
        self._update_service_controls()

    def _sync_service_tree(self) -> None:
        rows: list[list[QStandardItem]] = []
        names: list[str] = []
        statuses: dict[str, str] = {}
        if self.service_manager is not None:
            for service in self.service_manager.get_status():
                name = str(service["name"])
                status = str(service["status"])
                names.append(name)
                statuses[name] = status
                row = [
                    _read_only_item(name),
                    _read_only_item(status),
                    _read_only_item("-" if service["pid"] is None else service["pid"]),
                    _read_only_item(service["restart_count"]),
                    _read_only_item(
                        "-" if service["exit_code"] is None else service["exit_code"]
                    ),
                ]
                row[0].setData(name, Qt.ItemDataRole.UserRole)
                color = {
                    "running": QColor("#08752f"),
                    "crashed": QColor("#b42318"),
                    "disabled": QColor("#98a2b3"),
                    "unmanaged": QColor("#667085"),
                }.get(status)
                if color is not None:
                    row[1].setForeground(color)
                rows.append(row)

        model = self.window.service_model
        selection = self.window.service_tree.selectionModel()
        with QSignalBlocker(selection):
            model.removeRows(0, model.rowCount())
            for row in rows:
                model.appendRow(row)
            self._display_names = names
            self._status_by_name = statuses
            if self._selected_name in names:
                index = model.index(names.index(self._selected_name), 0)
                selection.select(
                    index,
                    QItemSelectionModel.SelectionFlag.ClearAndSelect
                    | QItemSelectionModel.SelectionFlag.Rows,
                )
                self.window.service_tree.setCurrentIndex(index)
            else:
                self._selected_name = None

    def _update_service_controls(self) -> None:
        name = self._selected_name
        status = self._status_by_name.get(name, "") if name else ""
        controllable = bool(name) and status in _CONTROLLABLE
        running = status == "running"
        self.window.start_service_button.setEnabled(controllable and not running)
        self.window.stop_service_button.setEnabled(controllable and running)
        self.window.restart_service_button.setEnabled(controllable and running)
        if not name:
            caption = "Selected service — click a row to control it"
        elif controllable:
            caption = f"Selected service: {name} ({status})"
        else:
            caption = f"Selected service: {name} ({status} — not controllable)"
        self.window.service_controls.setTitle(caption)

    def _on_row_selected(
        self, selected: QItemSelection, _deselected: QItemSelection
    ) -> None:
        indexes = selected.indexes()
        if indexes:
            index = indexes[0].siblingAtColumn(0)
            value = index.data(Qt.ItemDataRole.UserRole)
            self._selected_name = str(value) if value is not None else None
        elif not self.window.service_tree.selectionModel().hasSelection():
            self._selected_name = None
        self._update_service_controls()

    def _act(self, method: str) -> None:
        manager = self.service_manager
        name = self._selected_name
        if manager is None or not self.running:
            return
        if not name:
            self._set_banner("Select a service first.")
            return
        try:
            getattr(manager, method)(name)
            self._set_banner("")
        except KeyError as exc:
            self._set_banner(f"Error: {exc}")
        self._refresh_service_view()

    # --- lifecycle toggle -----------------------------------------------------

    def _stop_to_edit(self) -> None:
        self._set_status("stopping…")
        self.stop_services()
        self._set_status("stopped — editing")
        self._sync_run_state()

    def _start_all(self) -> None:
        if not self._persist():
            return
        self._set_status("starting…")
        try:
            self.start_services()
        except Exception as exc:  # noqa: BLE001 - surface config/runtime errors
            self._set_status(f"start failed: {exc}", _ERROR)
            return
        self._set_status("saved & started ✓", _SUCCESS)
        self._reload_routes()
        self._populate_log_services()
        self._sync_run_state()

    # --- config editor --------------------------------------------------------

    def _load_file(self) -> None:
        try:
            content = self.paths.services_file.read_text()
        except OSError as exc:
            content = ""
            self._set_status(f"read error: {exc}", _ERROR)
        with QSignalBlocker(self.window.config_editor):
            self.window.config_editor.setPlainText(content)
        self.window.readonly_config.setPlainText(content)
        self._loaded_config = content
        self.window.set_dirty(False)

    def _reload(self) -> None:
        self._load_file()
        self._set_banner(self._mode_banner)
        self._set_status("reloaded from disk")

    def _on_config_edited(self) -> None:
        dirty = self.window.config_editor.toPlainText() != self._loaded_config
        self.window.set_dirty(dirty)

    def _validate(self) -> bool:
        try:
            validate_toml(self.window.config_editor.toPlainText())
        except RouteConfigError as exc:
            self._set_status("invalid", _ERROR)
            self._set_banner(str(exc))
            return False
        self._set_status("valid ✓", _SUCCESS)
        self._set_banner(self._mode_banner)
        return True

    def _persist(self) -> bool:
        if self.running:
            self._set_status("stop services first", _ERROR)
            return False
        if not self._validate():
            return False
        text = self.window.config_editor.toPlainText()
        destination = self.paths.services_file
        temporary = destination.with_name(f"{destination.name}.tmp")
        try:
            temporary.write_text(text)
            os.replace(temporary, destination)
        except OSError as exc:
            self._set_status(f"write error: {exc}", _ERROR)
            return False
        self._loaded_config = text
        self.window.set_dirty(False)
        return True

    def _save(self) -> None:
        if self._persist():
            self._set_status("saved ✓", _SUCCESS)

    # --- logs ----------------------------------------------------------------

    def _populate_log_services(self) -> None:
        names = self.service_manager.service_names() if self.service_manager else []
        combo = self.window.log_service_combo
        current_name = combo.currentText()
        with QSignalBlocker(combo):
            combo.clear()
            combo.addItems(names)
            if current_name in names:
                combo.setCurrentText(current_name)
            elif names:
                combo.setCurrentIndex(0)
        self._log_names = names

    def _refresh_logs(self) -> None:
        self._populate_log_services()
        manager = self.service_manager
        index = self.window.log_service_combo.currentIndex()
        if manager is None or not (0 <= index < len(self._log_names)):
            self.window.log_text.clear()
            return
        name = self._log_names[index]
        try:
            body = _tail_file(
                manager.get_log_path(name), self.window.log_lines_spin.value()
            )
        except KeyError as exc:
            body = f"[error] {exc}"
        self.window.log_text.setPlainText(body)
        QTimer.singleShot(0, self._pin_log_to_bottom)

    def _pin_log_to_bottom(self) -> None:
        scrollbar = self.window.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_select_log(self, _index: int) -> None:
        self._refresh_logs()

    def _on_follow_toggled(self, checked: bool) -> None:
        if checked:
            self._refresh_logs()

    # --- routes ---------------------------------------------------------------

    def _reload_routes(self) -> None:
        model = self.window.route_model
        model.removeRows(0, model.rowCount())
        try:
            manifest = load_routes(self.paths.services_file)
        except RouteConfigError as exc:
            self.window.set_routes_banner(f"Error: {exc}")
            return
        self.window.set_routes_banner("")
        for service in manifest.services.values():
            self._add_route_service(service, manifest.http_port)
        self.window.route_tree.expandAll()

    def _add_route_service(self, service: ServiceDef, http_port: int) -> None:
        if service.disabled:
            note = "  (disabled — routes inactive)"
        elif service.command is None:
            note = "  (external — not started here)"
        else:
            note = ""
        service_item = _read_only_item(f"{service.name}{note}")
        service_item.setFont(QFont(service_item.font().family(), -1, QFont.Weight.Bold))
        row = [service_item, _read_only_item(""), _read_only_item("")]
        if service.disabled:
            for item in row:
                item.setForeground(QColor("#98a2b3"))
        self.window.route_model.appendRow(row)
        if service.disabled:
            return
        for route in service.routes:
            self._add_route_rows(service_item, service, route, http_port)

    def _add_route_rows(
        self,
        parent: QStandardItem,
        service: ServiceDef,
        route: ServiceRoute,
        http_port: int,
    ) -> None:
        target = self._target(service, route)
        for host in route.hosts:
            wildcard = "*" in host
            url = "" if wildcard else f"http://{host}:{http_port}/"
            shown_url = f"{host}  (wildcard)" if wildcard else url
            name_item = _read_only_item(route.id)
            url_item = _read_only_item(shown_url)
            target_item = _read_only_item(target)
            for item in (name_item, url_item, target_item):
                item.setData(url, Qt.ItemDataRole.UserRole)
                if wildcard:
                    item.setForeground(QColor("#98a2b3"))
            if url:
                url_item.setForeground(QColor("#175cd3"))
                url_item.setToolTip(f"Open {url}")
            parent.appendRow([name_item, url_item, target_item])

    @staticmethod
    def _target(service: ServiceDef, route: ServiceRoute) -> str:
        if route.target_socket is not None:
            return f"unix:{route.target_socket}"
        if route.target_socket_env is not None:
            socket_path = service.env.get(route.target_socket_env)
            return (
                f"unix:{socket_path}"
                if socket_path
                else f"unix:${{{route.target_socket_env}}}"
            )

        assert route.target_host is not None
        if route.target_port is not None:
            return f"{route.target_host}:{route.target_port}"
        env_name = route.target_port_env
        port = service.env.get(env_name) if env_name else None
        return (
            f"{route.target_host}:{port}"
            if port
            else f"{route.target_host}:${{{env_name}}}"
        )

    def _open_route_index(self, index: QModelIndex) -> None:
        url = index.data(Qt.ItemDataRole.UserRole)
        if isinstance(url, str):
            self._open_url(url)

    def _open_url(self, url: str) -> None:
        if url.startswith(("http://", "https://")):
            self._url_opener(url)


def run_gui() -> None:
    paths = ensure_config()
    lock = _acquire_lock()
    app = QApplication.instance()
    owns_application = app is None
    if app is None:
        app = QApplication(sys.argv)
    assert isinstance(app, QApplication)
    app.setApplicationName("Local Dev Proxy")
    app.setOrganizationName("local-dev-proxy")
    app.setQuitOnLastWindowClosed(False)

    controller = ManagerController(paths, lock, application=app)
    try:
        controller.start_services()
    except Exception as exc:  # noqa: BLE001 - bad config opens in editor mode
        print(f"Startup failed; services left stopped: {exc}", file=sys.stderr)

    controller.prime()
    controller.window.show()
    if QSystemTrayIcon.isSystemTrayAvailable():
        controller.tray.show()
    controller.install_signals()
    controller.start_timers()

    try:
        if owns_application:
            app.exec()
    finally:
        controller.quit()
