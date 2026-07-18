"""The manager UI, built on Slint (cross-platform: macOS, Windows, Linux).

A single Slint event loop hosts the manager ``Window`` and a ``SystemTrayIcon``
with a dropdown menu. The ``ManagerController`` owns the in-process reverse proxy
and the managed service subprocesses and wires the Slint view (``ui/manager.slint``)
to that business logic. Closing the window hides it to the tray (the tray keeps
the loop alive); Quit stops everything and exits.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
import threading
import webbrowser
from datetime import timedelta
from importlib import resources
from pathlib import Path

import slint

from . import __version__
from .config import (
    AlreadyRunningError,
    ProjectPaths,
    acquire_instance_lock,
    bundled_resource,
    consume_raise_request,
    dock_icon_path,
    ensure_config,
    icon_path,
    release_instance_lock,
)
from .routes import RouteConfigError, load_routes, validate_toml
from .services import start_proxy, start_services_managed

logger = logging.getLogger(__name__)

_POLL_MS = 2000  # tab refresh cadence
_SIGNAL_POLL_MS = 300  # shutdown-signal / raise-request cadence

# Statuses whose service can be started/stopped/restarted individually.
_CONTROLLABLE = frozenset({"running", "stopped", "crashed"})

# status_level values shared with the .slint view.
_NEUTRAL, _SUCCESS, _ERROR = 0, 1, 2


def _acquire_lock() -> object:
    """Take the single-instance lock; exit if another manager already holds it."""
    try:
        return acquire_instance_lock()
    except AlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)


def _tail_file(path: Path, lines: int) -> str:
    if not path.exists() or lines <= 0:
        return ""
    # Read backwards in chunks so we only load roughly the requested tail into
    # memory rather than the whole (potentially huge) log file.
    chunk = 8192
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            buf = b""
            while pos > 0 and buf.count(b"\n") <= lines:
                read = min(chunk, pos)
                pos -= read
                fh.seek(pos)
                buf = fh.read(read) + buf
    except OSError:
        return ""
    text = buf.decode(errors="replace")
    data = text.splitlines()
    tail = data[-lines:] if len(data) > lines else data
    return "\n".join(tail) + ("\n" if tail else "")


def _load_ui() -> object:
    """Compile ui/manager.slint (works from a source tree or a wheel).

    Force the Fluent style so the UI looks identical on macOS, Windows, and Linux.
    """
    with resources.as_file(bundled_resource("ui/manager.slint")) as path:
        return slint.load_file(str(path), style="fluent")


def _load_image(path: Path | None) -> object | None:
    if path is None:
        return None
    try:
        return slint.Image.load_from_path(str(path))
    except Exception:  # noqa: BLE001 — a missing/broken icon must not crash startup
        logger.warning("Could not load icon from %s", path)
        return None


class ManagerController:
    """Owns the proxy + service manager and drives the Slint window and tray."""

    def __init__(self, paths: ProjectPaths, lock: object) -> None:
        self.paths = paths
        self._lock: object | None = lock
        self.service_manager = None
        self.proxy = None
        self.running = False
        self._quitting = False

        # View/selection state mirrored into the .slint view.
        self._last_running: bool | None = None
        self._selected_name: str | None = None
        self._display_names: list[str] = []
        self._status_by_name: dict[str, str] = {}
        self._log_names: list[str] = []
        self._loaded_config = ""  # last text loaded from / written to disk (dirty check)

        # Cross-thread signal → UI-thread quit (polled by the signal timer).
        self._shutdown_flag = threading.Event()

        ui = _load_ui()
        self.window = ui.MainWindow()
        self.tray = ui.TrayIcon()
        self.window.version = __version__
        app_icon = _load_image(dock_icon_path())
        if app_icon is not None:
            self.window.app_icon = app_icon
        tray_icon = _load_image(icon_path())
        if tray_icon is not None:
            self.tray.tray_icon = tray_icon

        self._bind_callbacks()

        self._refresh_timer = slint.Timer()
        self._signal_timer = slint.Timer()
        # Deferred (post-layout) scroll of the log view to its newest line.
        self._scroll_timer = slint.Timer()

        # Last-resort safety net: any interpreter exit still stops the children so
        # services never outlive the app. Idempotent.
        atexit.register(self._stop_children_safely)

    def _bind_callbacks(self) -> None:
        w = self.window
        w.quit = self.quit
        w.show_readonly = lambda: self._set_view("readonly")
        w.show_services = lambda: self._set_view("services")
        w.stop_to_edit = self._stop_to_edit
        w.start_all = self._start_all
        w.validate = lambda: self._validate()
        w.save = self._save
        w.reload_file = self._reload
        w.start_service = lambda: self._act("start_service")
        w.stop_service = lambda: self._act("stop_service")
        w.restart_service = lambda: self._act("restart_service")
        w.row_selected = self._on_row_selected
        w.config_edited = self._on_config_edited
        w.select_log = self._on_select_log
        w.follow_toggled = self._on_follow_toggled
        w.refresh_logs = self._refresh_logs
        w.reload_routes = self._reload_routes
        w.open_url = self._open_url
        self.tray.open_window = self._show_window
        self.tray.quit_app = self.quit

    # --- in-process lifecycle -------------------------------------------------

    def start_services(self) -> None:
        """(Re)build the manager + proxy from the current config and start them."""
        if self.running:
            return
        try:
            self.service_manager = start_services_managed(self.paths)
            self.service_manager.start_all()
            self.proxy = start_proxy(self.paths)
        except Exception:
            # A partial start leaves children alive; tear them down so a retry
            # doesn't launch duplicates.
            self.stop_services()
            raise
        self.running = True

    def stop_services(self) -> None:
        """Stop the proxy and all managed services (never raises)."""
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
        except Exception:  # noqa: BLE001 — exit-time best effort
            pass

    # --- lifecycle / event loop ----------------------------------------------

    def prime(self) -> None:
        """Populate the view from the initial run-state, before the first frame."""
        self._populate_log_services()
        self._reload_routes()
        self._refresh()

    def start_timers(self) -> None:
        self._refresh_timer.start(
            slint.TimerMode.Repeated, timedelta(milliseconds=_POLL_MS), self._refresh
        )
        self._signal_timer.start(
            slint.TimerMode.Repeated,
            timedelta(milliseconds=_SIGNAL_POLL_MS),
            self._poll_signals,
        )

    def install_signals(self) -> None:
        # Any terminating signal we can catch flags an orderly quit, picked up by
        # the signal timer on the UI thread. SIGHUP/SIGUSR* are absent on Windows.
        for signame in ("SIGTERM", "SIGINT", "SIGHUP"):
            sig = getattr(signal, signame, None)
            if sig is not None:
                signal.signal(sig, lambda *_: self._shutdown_flag.set())

    def _poll_signals(self) -> None:
        if self._shutdown_flag.is_set():
            self.quit()
            return
        if consume_raise_request():
            self._show_window()

    def _show_window(self) -> None:
        self.window.show()

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.stop_services()
        if self._lock is not None:
            release_instance_lock(self._lock)
            self._lock = None
        slint.quit_event_loop()

    # --- view / refresh -------------------------------------------------------

    def _set_status(self, text: str, level: int = _NEUTRAL) -> None:
        self.window.status_text = text
        self.window.status_level = level

    def _set_view(self, view: str) -> None:
        """Swap the Services tab between the list / read-only config / editor."""
        self.window.view = view
        if view == "services":
            self.window.banner = ""
            self._refresh_service_view()
        elif view == "readonly":
            self._load_file()  # reflect the current on-disk config
            self.window.banner = "Viewing configuration (read-only) — services still running."
        else:  # edit
            self._load_file()
            self.window.banner = "Editing configuration — Start All to validate, save, and launch."
        # Logs/Routes are meaningful only while services run (i.e. not in edit mode).
        self.window.editing = view == "edit"

    def _sync_run_state(self) -> None:
        """On a run-state change, pick the natural view (running -> list, else edit)."""
        if self.running != self._last_running:
            self._set_view("services" if self.running else "edit")
            self._last_running = self.running

    def _refresh(self) -> None:
        self._sync_run_state()
        if self.window.view == "services":
            self._refresh_service_view()
        if self.running and self.window.log_follow:
            self._refresh_logs()

    def _refresh_service_view(self) -> None:
        self._sync_service_table()
        self._update_service_controls()

    def _sync_service_table(self) -> None:
        rows = []
        names = []
        status_by_name = {}
        sm = self.service_manager
        if sm is not None:
            for svc in sm.get_status():
                name = str(svc["name"])
                names.append(name)
                status_by_name[name] = str(svc["status"])
                # A StandardTableView row is a model of StandardListViewItem cells.
                rows.append(
                    slint.ListModel(
                        [
                            {"text": name},
                            {"text": str(svc["status"])},
                            {"text": "-" if svc["pid"] is None else str(svc["pid"])},
                            {"text": str(svc["restart_count"])},
                            {"text": "-" if svc["exit_code"] is None else str(svc["exit_code"])},
                        ]
                    )
                )
        self._display_names = names
        self._status_by_name = status_by_name
        self.window.service_rows = slint.ListModel(rows)
        # Preserve selection by name across the rebuild.
        if self._selected_name in names:
            self.window.selected_row = names.index(self._selected_name)
        else:
            self.window.selected_row = -1
            self._selected_name = None

    def _update_service_controls(self) -> None:
        name = self._selected_name
        status = self._status_by_name.get(name, "") if name else ""
        controllable = bool(name) and status in _CONTROLLABLE
        is_running = status == "running"
        self.window.can_start = controllable and not is_running
        self.window.can_stop = controllable and is_running
        self.window.can_restart = controllable and is_running
        if not name:
            self.window.sel_caption = "Selected service — click a row to control it"
        elif controllable:
            self.window.sel_caption = f"Selected service: {name} ({status})"
        else:
            self.window.sel_caption = f"Selected service: {name} ({status} — not controllable)"

    def _on_row_selected(self, idx: int) -> None:
        if 0 <= idx < len(self._display_names):
            self._selected_name = self._display_names[idx]
        else:
            self._selected_name = None
        self._update_service_controls()

    def _act(self, method: str) -> None:
        sm = self.service_manager
        name = self._selected_name
        if sm is None or not self.running:
            return
        if not name:
            self.window.banner = "Select a service first."
            return
        try:
            getattr(sm, method)(name)
            self.window.banner = ""
        except KeyError as exc:
            self.window.banner = f"Error: {exc}"
        self._refresh_service_view()

    # --- lifecycle toggle -----------------------------------------------------

    def _stop_to_edit(self) -> None:
        self._set_status("stopping…", _NEUTRAL)
        self.stop_services()
        self._set_status("stopped — editing", _NEUTRAL)
        self._sync_run_state()  # running is now False -> switches to the editor

    def _start_all(self) -> None:
        # Start always saves; save always validates (see _persist / _validate).
        if not self._persist():
            return
        self._set_status("starting…", _NEUTRAL)
        try:
            self.start_services()
        except Exception as exc:  # noqa: BLE001 — config may be invalid at start time
            self._set_status(f"start failed: {exc}", _ERROR)
            return
        self._set_status("saved & started ✓", _SUCCESS)
        self._reload_routes()  # config may have changed while editing
        self._populate_log_services()
        self._sync_run_state()  # running is now True -> switches to the service list

    # --- config editor --------------------------------------------------------

    def _load_file(self) -> None:
        try:
            content = self.paths.services_file.read_text()
        except OSError as exc:
            content = ""
            self._set_status(f"read error: {exc}", _ERROR)
        self.window.config_text = content
        self._loaded_config = content
        self.window.dirty = False

    def _reload(self) -> None:
        self._load_file()
        self._set_status("reloaded from disk", _NEUTRAL)

    def _on_config_edited(self) -> None:
        self.window.dirty = self.window.config_text != self._loaded_config

    def _validate(self) -> bool:
        text = self.window.config_text
        try:
            validate_toml(text)
        except RouteConfigError as exc:
            self._set_status("invalid", _ERROR)
            self.window.banner = str(exc)
            return False
        self._set_status("valid ✓", _SUCCESS)
        return True

    def _persist(self) -> bool:
        """Validate the editor buffer and write it to disk. Returns True on success."""
        if self.running:
            self._set_status("stop services first", _ERROR)
            return False
        if not self._validate():
            return False
        text = self.window.config_text
        dest = self.paths.services_file
        try:
            # Write to a temp sibling then atomically replace, so a failed write
            # never leaves services.toml half-written / corrupted.
            tmp = dest.with_name(f"{dest.name}.tmp")
            tmp.write_text(text)
            os.replace(tmp, dest)
        except OSError as exc:
            self._set_status(f"write error: {exc}", _ERROR)
            return False
        self._loaded_config = text
        self.window.dirty = False
        return True

    def _save(self) -> None:
        if self._persist():
            self._set_status("saved ✓", _SUCCESS)

    # --- logs -----------------------------------------------------------------

    def _populate_log_services(self) -> None:
        sm = self.service_manager
        names = sm.service_names() if sm is not None else []
        self._log_names = names
        self.window.log_services = slint.ListModel(names)
        current = self.window.log_current
        if not (0 <= current < len(names)):
            self.window.log_current = 0 if names else -1

    def _refresh_logs(self) -> None:
        self._populate_log_services()
        sm = self.service_manager
        idx = self.window.log_current
        if sm is None or not (0 <= idx < len(self._log_names)):
            self.window.log_text = ""
            return
        name = self._log_names[idx]
        lines = self.window.log_lines
        try:
            body = _tail_file(sm.get_log_path(name), lines)
        except KeyError as exc:
            body = f"[error] {exc}"
        self.window.log_text = body
        self._pin_log_to_bottom()

    def _pin_log_to_bottom(self) -> None:
        # Defer a frame so the viewport height reflects the just-set text before
        # we scroll to the newest line (mirrors the old Tk see("end")).
        self._scroll_timer.start(
            slint.TimerMode.SingleShot,
            timedelta(milliseconds=16),
            lambda: self.window.scroll_log_bottom(),
        )

    def _on_select_log(self, _idx: int) -> None:
        self._refresh_logs()

    def _on_follow_toggled(self, checked: bool) -> None:
        if checked:
            self._refresh_logs()

    # --- routes ---------------------------------------------------------------

    def _reload_routes(self) -> None:
        try:
            manifest = load_routes(self.paths.services_file)
        except RouteConfigError as exc:
            self.window.routes_banner = f"Error: {exc}"
            self.window.route_rows = slint.ListModel([])
            return
        self.window.routes_banner = ""
        rows: list[dict[str, object]] = []
        for service in manifest.services.values():
            rows.extend(self._route_rows_for(service, manifest.http_port))
        self.window.route_rows = slint.ListModel(rows)

    def _route_rows_for(self, service, http_port: int) -> list[dict[str, object]]:
        # A service header, then its routes nested beneath (indented in the view).
        if service.disabled:
            note, muted = "  (disabled — routes inactive)", True
        elif service.command is None:
            note, muted = "  (external — not started here)", False
        else:
            note, muted = "", False
        rows: list[dict[str, object]] = [
            {"kind": "service-header", "text": f"{service.name}{note}",
             "url": "", "target": "", "muted": muted}
        ]
        if service.disabled:
            # Disabled services' routes are excluded from the proxy — header only.
            return rows
        for route in service.routes:
            target = self._target(service, route)
            for host in route.hosts:
                if "*" in host:
                    rows.append({"kind": "route", "text": route.id, "url": "",
                                 "target": f"{host}  (wildcard)  →  {target}", "muted": True})
                else:
                    rows.append({"kind": "route", "text": route.id,
                                 "url": f"http://{host}:{http_port}/", "target": target,
                                 "muted": False})
        return rows

    @staticmethod
    def _target(service, route) -> str:
        host = route.target_host
        if route.target_port is not None:
            return f"{host}:{route.target_port}"
        env_name = route.target_port_env
        port = service.env.get(env_name)
        return f"{host}:{port}" if port else f"{host}:${{{env_name}}}"

    def _open_url(self, url: str) -> None:
        if url.startswith("http://") or url.startswith("https://"):
            webbrowser.open(url)


def run_gui() -> None:
    paths = ensure_config()
    lock = _acquire_lock()

    controller = ManagerController(paths, lock)
    # Start services before the first frame so the Services tab paints in its
    # running (list) view. start_services() rolls back on failure (running stays
    # False), so a bad config simply opens the tab in edit mode; the window comes
    # up either way.
    try:
        controller.start_services()
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the window
        print(f"Startup failed; services left stopped: {exc}", file=sys.stderr)

    controller.prime()
    controller.window.show()
    controller.install_signals()
    controller.start_timers()

    try:
        slint.run_event_loop()
    finally:
        # Guarantee teardown even if the loop exits abnormally so services never
        # outlive the app.
        controller.quit()
