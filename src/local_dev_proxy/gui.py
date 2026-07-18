from __future__ import annotations

import fcntl
import os
import signal
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk

from .config import LOCK_PATH, ProjectPaths, ensure_config, icon_path
from .process_manager import ServiceManager
from .proxy import ProxyServer
from .routes import RouteConfigError, load_routes, validate_toml
from .services import start_proxy, start_services_managed
from .status_item import install_status_item, remove_status_item

_POLL_MS = 2000  # tab refresh cadence
_SIGNAL_POLL_MS = 300  # signal / raise-window flag cadence


def _acquire_lock() -> int:
    """Take the single-instance lock; exit if another manager already holds it."""
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        print("local-dev-proxy is already running.", file=sys.stderr)
        sys.exit(1)
    return fd


def _tail_file(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    data = path.read_text(errors="replace").splitlines()
    tail = data[-lines:] if len(data) > lines else data
    return "\n".join(tail) + ("\n" if tail else "")


class ManagerApp:
    """The single-process app: owns the proxy + service manager and the window."""

    def __init__(self, root: tk.Tk, paths: ProjectPaths, lock_fd: int) -> None:
        self.root = root
        self.paths = paths
        self._lock_fd: int | None = lock_fd
        self.service_manager: ServiceManager | None = None
        self.proxy: ProxyServer | None = None
        self.running = False

        self._status_handle: object | None = None
        self._raise_flag = threading.Event()
        self._shutdown_flag = threading.Event()
        self._quitting = False

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
        """Stop the proxy and all managed services (keeps the window open)."""
        if self.proxy is not None:
            self.proxy.stop()
            self.proxy = None
        if self.service_manager is not None:
            self.service_manager.stop_all()
        self.running = False

    # --- UI -------------------------------------------------------------------

    def build_ui(self) -> None:
        toolbar = ttk.Frame(self.root, padding=(8, 6, 8, 0))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Quit", command=self.quit).pack(side="right")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)
        notebook.add(ServicesTab(notebook, self), text="Services")
        notebook.add(LogsTab(notebook, self), text="Logs")
        notebook.add(RoutesTab(notebook, self.paths), text="Routes")
        notebook.add(ConfigTab(notebook, self), text="Config")

    def install_icon(self) -> None:
        self._status_handle = install_status_item(icon_path(), self._raise_flag.set)

    # --- signals / window plumbing -------------------------------------------

    def install_signals(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self._shutdown_flag.set())
        signal.signal(signal.SIGINT, lambda *_: self._shutdown_flag.set())
        signal.signal(signal.SIGUSR1, lambda *_: self._raise_flag.set())
        self._poll_signals()

    def wire_lifecycle(self) -> None:
        self.root.protocol("WM_DELETE_WINDOW", self._hide_window)
        self.root.bind_all("<Command-q>", lambda _e: self.quit())
        # macOS app-menu Quit / ⌘Q route here when defined.
        with_mac_quit = getattr(self.root, "createcommand", None)
        if with_mac_quit is not None:
            try:
                self.root.createcommand("tk::mac::Quit", self.quit)
            except tk.TclError:
                pass

    def _poll_signals(self) -> None:
        if self._shutdown_flag.is_set():
            self.quit()
            return
        if self._raise_flag.is_set():
            self._raise_flag.clear()
            self._show_window()
        self.root.after(_SIGNAL_POLL_MS, self._poll_signals)

    def _hide_window(self) -> None:
        self.root.withdraw()

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.stop_services()
        remove_status_item(self._status_handle)
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        self.root.destroy()


class ServicesTab(ttk.Frame):
    COLUMNS = ("status", "pid", "restarts", "exit")

    def __init__(self, master: tk.Misc, app: ManagerApp) -> None:
        super().__init__(master, padding=8)
        self._app = app

        self._banner = ttk.Label(self, text="", foreground="#b00")
        self._banner.pack(fill="x", pady=(0, 4))

        self._tree = ttk.Treeview(self, columns=self.COLUMNS, show="tree headings", height=8)
        self._tree.heading("#0", text="service")
        self._tree.heading("status", text="status")
        self._tree.heading("pid", text="pid")
        self._tree.heading("restarts", text="restarts")
        self._tree.heading("exit", text="exit code")
        self._tree.column("#0", width=160, anchor="w")
        for col in self.COLUMNS:
            self._tree.column(col, width=90, anchor="center")
        self._tree.pack(fill="both", expand=True)

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=(6, 0))
        self._start_btn = ttk.Button(buttons, text="Start", command=lambda: self._act("start_service"))
        self._stop_btn = ttk.Button(buttons, text="Stop", command=lambda: self._act("stop_service"))
        self._restart_btn = ttk.Button(buttons, text="Restart", command=lambda: self._act("restart_service"))
        for btn in (self._start_btn, self._stop_btn, self._restart_btn):
            btn.pack(side="left", padx=(0, 6))

        self.refresh()

    def _selected(self) -> str | None:
        sel = self._tree.selection()
        return self._tree.item(sel[0], "text") if sel else None

    def _act(self, method: str) -> None:
        sm = self._app.service_manager
        name = self._selected()
        if sm is None or not self._app.running:
            self._banner.config(text="Services are stopped.")
            return
        if not name:
            self._banner.config(text="Select a service first.")
            return
        try:
            getattr(sm, method)(name)
            self._banner.config(text="")
        except KeyError as exc:
            self._banner.config(text=f"Error: {exc}")
        self.refresh(reschedule=False)

    def refresh(self, reschedule: bool = True) -> None:
        sm = self._app.service_manager
        active = self._app.running and sm is not None
        for btn in (self._start_btn, self._stop_btn, self._restart_btn):
            btn.state(["!disabled"] if active else ["disabled"])

        self._tree.delete(*self._tree.get_children())
        if not active:
            self._banner.config(text="Services are stopped (Config tab → Start).")
        else:
            assert sm is not None
            for svc in sm.get_status():
                self._tree.insert(
                    "", "end", text=str(svc["name"]),
                    values=(
                        svc["status"],
                        svc["pid"] if svc["pid"] is not None else "-",
                        svc["restart_count"],
                        svc["exit_code"] if svc["exit_code"] is not None else "-",
                    ),
                )
            if self._banner.cget("text").startswith("Services are stopped"):
                self._banner.config(text="")

        if reschedule:
            self.after(_POLL_MS, self.refresh)


class LogsTab(ttk.Frame):
    def __init__(self, master: tk.Misc, app: ManagerApp) -> None:
        super().__init__(master, padding=8)
        self._app = app

        controls = ttk.Frame(self)
        controls.pack(fill="x")
        ttk.Label(controls, text="Service:").pack(side="left")
        self._service = ttk.Combobox(controls, state="readonly", width=20)
        self._service.pack(side="left", padx=(4, 12))
        ttk.Label(controls, text="Lines:").pack(side="left")
        self._lines = tk.Spinbox(controls, from_=10, to=5000, increment=10, width=6)
        self._lines.delete(0, "end")
        self._lines.insert(0, "200")
        self._lines.pack(side="left", padx=(4, 12))
        self._follow = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="Follow", variable=self._follow, command=self._tick).pack(side="left")
        ttk.Button(controls, text="Refresh", command=self._tick).pack(side="right")

        self._text = tk.Text(self, wrap="word", height=20, font=("Menlo", 9))
        self._text.pack(fill="both", expand=True, pady=(6, 0))
        yscroll = ttk.Scrollbar(self._text, command=self._text.yview)
        yscroll.pack(side="right", fill="y")
        self._text.config(yscrollcommand=yscroll.set, state="disabled")

        self._populate_services()
        self._tick()

    def _populate_services(self) -> None:
        sm = self._app.service_manager
        names = sm.service_names() if sm is not None else []
        self._service["values"] = names
        if names and not self._service.get():
            self._service.current(0)

    def _tick(self) -> None:
        sm = self._app.service_manager
        name = self._service.get()
        if not name:
            self._populate_services()
            name = self._service.get()
        if name and sm is not None:
            try:
                lines = int(self._lines.get())
            except ValueError:
                lines = 200
            try:
                body = _tail_file(sm.get_log_path(name), lines)
            except KeyError as exc:
                body = f"[error] {exc}"
            self._set_text(body)

        if self._follow.get():
            self.after(_POLL_MS, self._tick)

    def _set_text(self, body: str) -> None:
        self._text.config(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("1.0", body)
        self._text.config(state="disabled")
        # Pin to the newest line (bottom), like `tail -f`.
        self._text.see("end")


class RoutesTab(ttk.Frame):
    def __init__(self, master: tk.Misc, paths: ProjectPaths) -> None:
        super().__init__(master, padding=8)
        self._paths = paths

        ttk.Label(self, text="Double-click a route to open it in your browser.").pack(anchor="w")
        self._banner = ttk.Label(self, text="", foreground="#b00")
        self._banner.pack(fill="x")

        self._tree = ttk.Treeview(self, columns=("url",), show="tree headings", height=12)
        self._tree.heading("#0", text="route")
        self._tree.heading("url", text="url")
        self._tree.column("#0", width=160, anchor="w")
        self._tree.column("url", width=360, anchor="w")
        self._tree.pack(fill="both", expand=True, pady=(6, 0))
        self._tree.bind("<Double-1>", self._open)

        ttk.Button(self, text="Reload", command=self.refresh).pack(anchor="e", pady=(6, 0))
        self.refresh()

    def refresh(self) -> None:
        self._tree.delete(*self._tree.get_children())
        try:
            manifest = load_routes(self._paths.services_file)
        except RouteConfigError as exc:
            self._banner.config(text=f"Error: {exc}")
            return
        self._banner.config(text="")
        for service in manifest.services.values():
            for route in service.routes:
                for host in route.hosts:
                    if "*" in host:
                        continue
                    url = f"http://{host}:{manifest.http_port}/"
                    self._tree.insert("", "end", text=route.id, values=(url,))

    def _open(self, _event: tk.Event) -> None:
        sel = self._tree.selection()
        if sel:
            url = self._tree.item(sel[0], "values")[0]
            webbrowser.open(url)


class ConfigTab(ttk.Frame):
    def __init__(self, master: tk.Misc, app: ManagerApp) -> None:
        super().__init__(master, padding=8)
        self._app = app

        self._banner = ttk.Label(self, text="", foreground="#b00")
        self._banner.pack(fill="x")

        lifecycle = ttk.Frame(self)
        lifecycle.pack(fill="x", pady=(0, 4))
        self._stop_btn = ttk.Button(lifecycle, text="Stop to Edit", command=self._stop)
        self._start_btn = ttk.Button(lifecycle, text="Start", command=self._start)
        self._stop_btn.pack(side="left", padx=(0, 6))
        self._start_btn.pack(side="left")

        self._text = tk.Text(self, wrap="none", height=22, font=("Menlo", 11))
        self._text.pack(fill="both", expand=True)

        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(6, 0))
        self._validate_btn = ttk.Button(actions, text="Validate", command=self._validate)
        self._save_btn = ttk.Button(actions, text="Save", command=self._save)
        self._reload_btn = ttk.Button(actions, text="Reload from disk", command=self._load_file)
        self._validate_btn.pack(side="left", padx=(0, 6))
        self._save_btn.pack(side="left", padx=(0, 6))
        self._reload_btn.pack(side="left")
        self._status = ttk.Label(actions, text="")
        self._status.pack(side="right")

        self._load_file()
        self.refresh()

    def _load_file(self) -> None:
        try:
            content = self._app.paths.services_file.read_text()
        except OSError as exc:
            content = ""
            self._status.config(text=f"read error: {exc}")
        self._text.delete("1.0", "end")
        self._text.insert("1.0", content)

    def refresh(self) -> None:
        running = self._app.running
        if running:
            self._banner.config(text="Services are running — stop them to edit the configuration.")
            self._text.config(state="disabled")
            self._stop_btn.state(["!disabled"])
            self._start_btn.state(["disabled"])
            for btn in (self._validate_btn, self._save_btn):
                btn.state(["disabled"])
        else:
            self._banner.config(text="Services are stopped — edit, then Start to apply (Start also saves).")
            self._text.config(state="normal")
            self._stop_btn.state(["disabled"])
            self._start_btn.state(["!disabled"])
            for btn in (self._validate_btn, self._save_btn):
                btn.state(["!disabled"])
        self.after(_POLL_MS, self.refresh)

    def _stop(self) -> None:
        self._status.config(text="stopping…")
        self._app.stop_services()
        self._status.config(text="stopped")
        self.refresh()

    def _start(self) -> None:
        # Persist the editor buffer first so Start always applies what's on screen.
        if not self._persist():
            return
        self._status.config(text="starting…")
        try:
            self._app.start_services()
        except Exception as exc:  # config may be invalid at start time
            self._status.config(text=f"start failed: {exc}", foreground="#b00")
            return
        self._status.config(text="saved & started ✓", foreground="#070")
        self.refresh()

    def _validate(self) -> bool:
        text = self._text.get("1.0", "end-1c")
        try:
            validate_toml(text)
        except RouteConfigError as exc:
            self._status.config(text="invalid", foreground="#b00")
            self._banner.config(text=str(exc))
            return False
        self._status.config(text="valid ✓", foreground="#070")
        return True

    def _persist(self) -> bool:
        """Validate the editor buffer and write it to disk. Returns True on success."""
        if self._app.running:
            self._status.config(text="stop services first", foreground="#b00")
            return False
        if not self._validate():
            return False
        text = self._text.get("1.0", "end-1c")
        try:
            self._app.paths.services_file.write_text(text)
        except OSError as exc:
            self._status.config(text=f"write error: {exc}", foreground="#b00")
            return False
        return True

    def _save(self) -> None:
        if self._persist():
            self._status.config(text="saved ✓", foreground="#070")


def run_gui() -> None:
    paths = ensure_config()
    lock_fd = _acquire_lock()

    root = tk.Tk()
    root.title("Local Dev Proxy — Manager")
    root.geometry("760x560")

    app = ManagerApp(root, paths, lock_fd)
    app.start_services()
    app.build_ui()
    app.install_icon()
    app.wire_lifecycle()
    app.install_signals()

    root.mainloop()
