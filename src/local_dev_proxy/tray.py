from __future__ import annotations

import atexit
import fcntl
import os
import sys
import threading
import webbrowser

import rumps

from .config import get_paths
from .routes import load_routes
from .services import (
    kill_zellij_session,
    start_caddy_background,
    start_zellij_headless,
)

SESSION_NAME = "local-dev-proxy"


class LocalDevProxyApp(rumps.App):
    def __init__(self) -> None:
        self._paths = get_paths()
        self._manifest = load_routes(self._paths.services_file)

        menu_items: list[rumps.MenuItem | None] = []
        self._url_map: dict[str, str] = {}

        http_port = self._manifest.caddy.http_port
        portal_item = rumps.MenuItem("Portal", callback=self._open_url)
        menu_items.append(portal_item)
        self._url_map["Portal"] = f"http://localhost:{http_port}/"
        menu_items.append(None)  # separator

        for service in self._manifest.services.values():
            for route in service.routes:
                host = route.hosts[0]
                url = f"http://{host}:{self._manifest.caddy.http_port}/"
                item = rumps.MenuItem(route.id, callback=self._open_url)
                menu_items.append(item)
                self._url_map[route.id] = url

        menu_items.append(None)  # separator

        icon_path = str(self._paths.root / "assets" / "tray-icon.png")
        super().__init__("LocalDevProxy", icon=icon_path, template=True, menu=menu_items, quit_button=None)
        self.title = None

        self._caddy_proc = start_caddy_background(self._paths)
        self._zellij_pid, self._zellij_fd = start_zellij_headless(
            self._paths, SESSION_NAME,
        )

        self._cleaned_up = False

        atexit.register(self._cleanup)

        self._monitor_thread = threading.Thread(
            target=self._monitor_caddy, daemon=True,
        )
        self._monitor_thread.start()

    def _open_url(self, sender: rumps.MenuItem) -> None:
        url = self._url_map.get(sender.title)
        if url:
            webbrowser.open(url)

    def _monitor_caddy(self) -> None:
        returncode = self._caddy_proc.wait()
        if returncode != 0:
            self._cleanup()
            rumps.notification(
                "LocalDevProxy",
                "Caddy crashed",
                f"Caddy exited with code {returncode}",
            )
            rumps.quit_application()

    @rumps.clicked("Quit")
    def on_quit(self, _sender: rumps.MenuItem) -> None:
        self._cleanup()
        rumps.quit_application()

    def _cleanup(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True

        if self._caddy_proc.poll() is None:
            self._caddy_proc.terminate()
            self._caddy_proc.wait(timeout=5)

        kill_zellij_session(SESSION_NAME)

        try:
            os.close(self._zellij_fd)
        except OSError:
            pass


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
    try:
        LocalDevProxyApp().run()
    finally:
        os.close(lock_fd)
