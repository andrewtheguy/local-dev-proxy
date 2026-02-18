from __future__ import annotations

import atexit
import fcntl
import os
import signal
import subprocess
import sys
import webbrowser

import rumps

from .config import get_paths
from .routes import load_routes
from .services import start_proxy, start_services_managed


class LocalDevProxyApp(rumps.App):
    def __init__(self) -> None:
        self._paths = get_paths()
        self._manifest = load_routes(self._paths.services_file)

        menu_items: list[rumps.MenuItem | None] = []
        self._url_map: dict[str, str] = {}

        http_port = self._manifest.http_port
        portal_item = rumps.MenuItem("Portal", callback=self._open_url)
        menu_items.append(portal_item)
        self._url_map["Portal"] = f"http://localhost:{http_port}/"
        menu_items.append(None)  # separator

        for service in self._manifest.services.values():
            for route in service.routes:
                host = route.hosts[0]
                url = f"http://{host}:{self._manifest.http_port}/"
                item = rumps.MenuItem(route.id, callback=self._open_url)
                menu_items.append(item)
                self._url_map[route.id] = url

        menu_items.append(None)  # separator
        menu_items.append(rumps.MenuItem("Open Logs Folder", callback=self._open_logs_folder))

        icon_path = str(self._paths.root / "assets" / "tray-icon.png")
        super().__init__("LocalDevProxy", icon=icon_path, template=True, menu=menu_items, quit_button=None)
        self.title = None

        self._service_manager = start_services_managed(self._paths)
        self._service_manager.start_all()

        self._proxy = start_proxy(self._paths, service_manager=self._service_manager)

        self._cleaned_up = False
        atexit.register(self._cleanup)

    def _open_url(self, sender: rumps.MenuItem) -> None:
        url = self._url_map.get(sender.title)
        if url:
            webbrowser.open(url)

    def _open_logs_folder(self, _sender: rumps.MenuItem) -> None:
        log_dir = self._paths.root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(log_dir)])

    @rumps.clicked("Quit")
    def on_quit(self, _sender: rumps.MenuItem) -> None:
        self._cleanup()
        rumps.quit_application()

    def _cleanup(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True

        self._proxy.stop()
        self._service_manager.stop_all()


_LOCK_PATH = os.path.join(os.environ.get("TMPDIR", "/tmp"), "local-dev-proxy.lock")


def _acquire_lock() -> int:
    """Acquire an exclusive lock file. Returns the fd (kept open for lifetime of process)."""
    fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        print("local-dev-proxy is already running.", file=sys.stderr)
        sys.exit(1)
    return fd


def run_tray() -> None:
    lock_fd = _acquire_lock()
    app = LocalDevProxyApp()

    def _signal_handler(signum: int, _frame: object) -> None:
        app._cleanup()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _signal_handler)

    try:
        app.run()
    finally:
        os.close(lock_fd)
