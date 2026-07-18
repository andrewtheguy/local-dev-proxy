from __future__ import annotations

import atexit
import fcntl
import logging
import os
import signal
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk

from .config import LOCK_PATH, ProjectPaths, dock_icon_path, ensure_config, icon_path
from .process_manager import ServiceManager
from .proxy import ProxyServer
from .routes import RouteConfigError, load_routes, validate_toml
from .services import start_proxy, start_services_managed
from .status_item import install_status_item, remove_status_item, set_dock_icon

logger = logging.getLogger(__name__)

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
            # +1 so we can drop a partial first line once we have enough.
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

        # Last-resort safety net: whatever path exits the interpreter (an
        # unhandled exception, sys.exit, mainloop returning), still stop the
        # child processes so services never outlive the app. Idempotent.
        atexit.register(self._stop_children_safely)

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
        """Stop the proxy and all managed services (keeps the window open).

        Never raises: the service teardown must run even if stopping the proxy
        fails, so child processes are never orphaned, and this is also the
        shutdown path — a proxy error here must not derail ``quit()``.
        """
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
                # Always clear running, even if manager teardown raised, so the
                # app's state stays consistent and quit() can proceed.
                self.running = False

    def _stop_children_safely(self) -> None:
        """atexit hook: guarantee child processes are stopped, swallowing errors."""
        try:
            self.stop_services()
        except Exception:  # noqa: BLE001 — exit-time best effort
            pass

    # --- UI -------------------------------------------------------------------

    def build_ui(self) -> None:
        toolbar = ttk.Frame(self.root, padding=(8, 6, 8, 0))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Quit", command=self.quit).pack(side="right")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)
        self._notebook = notebook
        self._services_tab = ServicesTab(notebook, self)
        self._logs_tab = LogsTab(notebook, self)
        self._routes_tab = RoutesTab(notebook, self.paths)
        notebook.add(self._services_tab, text="Services")
        notebook.add(self._logs_tab, text="Logs")
        notebook.add(self._routes_tab, text="Routes")
        # Sync tab availability with the initial run state.
        self.set_editing(not self.running)

    def set_editing(self, editing: bool) -> None:
        """While the config is being edited (services stopped), Logs and Routes
        show stale data, so disable them and keep focus on the Services tab."""
        notebook = getattr(self, "_notebook", None)
        if notebook is None or getattr(self, "_logs_tab", None) is None:
            return  # tabs not built yet (initial ServicesTab refresh)
        if editing:
            notebook.select(self._services_tab)
        state = "disabled" if editing else "normal"
        for tab in (self._logs_tab, self._routes_tab):
            notebook.tab(tab, state=state)

    def install_icon(self) -> None:
        set_dock_icon(dock_icon_path())
        self._status_handle = install_status_item(icon_path(), self._raise_flag.set)

    # --- signals / window plumbing -------------------------------------------

    def install_signals(self) -> None:
        # Any terminating signal we can catch routes to an orderly quit (which
        # stops all services). SIGHUP included so a session hangup tears down too.
        for signame in ("SIGTERM", "SIGINT", "SIGHUP"):
            sig = getattr(signal, signame, None)
            if sig is not None:
                signal.signal(sig, lambda *_: self._shutdown_flag.set())
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
        try:
            self.root.destroy()
        except tk.TclError:
            pass  # already torn down (e.g. quit() from the mainloop finally)


class ServicesTab(ttk.Frame):
    """Combined services + config tab, with three stacked views:

    * **services** — the live status list with per-service Start / Stop /
      Restart for the selected row (services running).
    * **readonly** — a read-only look at ``services.toml`` while services keep
      running. The top toggle flips between this and **services** without ever
      stopping anything; this view's own **Stop All & Edit Config** button is
      the only thing that stops services and opens the editor.
    * **edit** — the editable ``services.toml`` (services stopped); **Start All**
      validates, saves, and starts, returning to **services**.

    ``services``/``edit`` mirror ``app.running``; ``readonly`` is a UI-only
    detour available while running.
    """

    COLUMNS = ("status", "pid", "restarts", "exit")
    # Statuses whose service can be started/stopped/restarted individually.
    _CONTROLLABLE = frozenset({"running", "stopped", "crashed"})

    def __init__(self, master: tk.Misc, app: ManagerApp) -> None:
        super().__init__(master, padding=8)
        self._app = app
        self._view: str | None = None  # "services" | "readonly" | "edit"; None until first refresh
        self._last_running: bool | None = None  # detects external run-state changes in refresh()

        self._banner = ttk.Label(self, text="", foreground="#b00")
        self._banner.pack(fill="x", pady=(0, 4))

        lifecycle = ttk.Frame(self)
        lifecycle.pack(fill="x", pady=(0, 4))
        # Label + command are set per-view by _set_view(); this is just a placeholder.
        self._toggle_btn = ttk.Button(lifecycle, text="View Config", command=self._show_readonly)
        self._toggle_btn.pack(side="left")
        self._status = ttk.Label(lifecycle, text="")
        self._status.pack(side="right")

        # Three stacked views; _set_view() packs exactly one.
        self._body = ttk.Frame(self)
        self._body.pack(fill="both", expand=True)
        self._service_view = self._build_service_view(self._body)
        self._readonly_view = self._build_readonly_view(self._body)
        self._edit_view = self._build_edit_view(self._body)

        self._load_file()
        self.refresh()

    # --- view construction ----------------------------------------------------

    def _build_service_view(self, parent: tk.Misc) -> ttk.Frame:
        view = ttk.Frame(parent)
        self._tree = ttk.Treeview(view, columns=self.COLUMNS, show="tree headings", height=8)
        self._tree.heading("#0", text="service")
        self._tree.heading("status", text="status")
        self._tree.heading("pid", text="pid")
        self._tree.heading("restarts", text="restarts")
        self._tree.heading("exit", text="exit code")
        self._tree.column("#0", width=160, anchor="w")
        for col in self.COLUMNS:
            self._tree.column(col, width=90, anchor="center")
        self._tree.pack(fill="both", expand=True)
        self._tree.bind("<<TreeviewSelect>>", lambda _e: self._update_service_controls())

        # Per-service controls, clearly scoped to the selected row (vs. the
        # whole-app "Stop All & Edit Config" button at the top).
        self._sel_frame = ttk.LabelFrame(view, text="Selected service", padding=(8, 4))
        self._sel_frame.pack(fill="x", pady=(6, 0))
        self._start_btn = ttk.Button(self._sel_frame, text="Start", command=lambda: self._act("start_service"))
        self._stop_btn = ttk.Button(self._sel_frame, text="Stop", command=lambda: self._act("stop_service"))
        self._restart_btn = ttk.Button(self._sel_frame, text="Restart", command=lambda: self._act("restart_service"))
        for btn in (self._start_btn, self._stop_btn, self._restart_btn):
            btn.pack(side="left", padx=(0, 6))
        return view

    def _update_service_controls(self) -> None:
        """Enable each per-service button according to the selected row's status:
        Start only when it's not running, Stop/Restart only when it is."""
        name = self._selected()
        status = self._tree.set(name, "status") if name else ""
        controllable = bool(name) and status in self._CONTROLLABLE
        running = status == "running"

        def enable(btn: ttk.Button, on: bool) -> None:
            btn.state(["!disabled"] if on else ["disabled"])

        enable(self._start_btn, controllable and not running)
        enable(self._stop_btn, controllable and running)
        enable(self._restart_btn, controllable and running)

        if not name:
            self._sel_frame.config(text="Selected service — click a row to control it")
        elif controllable:
            self._sel_frame.config(text=f"Selected service: {name} ({status})")
        else:
            self._sel_frame.config(text=f"Selected service: {name} ({status} — not controllable)")

    def _build_readonly_view(self, parent: tk.Misc) -> ttk.Frame:
        view = ttk.Frame(parent)

        # Bottom-pinned action bar (like the editor) so the CTA stays visible.
        actions = ttk.Frame(view)
        actions.pack(side="bottom", fill="x", pady=(6, 0))
        self._edit_config_btn = ttk.Button(
            actions, text="Stop All & Edit Config", command=self._stop_to_edit
        )
        self._edit_config_btn.pack(side="left")

        self._ro_text = tk.Text(view, wrap="none", height=22, font=("Menlo", 11))
        self._ro_text.pack(side="top", fill="both", expand=True)
        self._ro_text.config(state="disabled")
        return view

    def _load_readonly(self) -> None:
        try:
            content = self._app.paths.services_file.read_text()
        except OSError as exc:
            content = f"# read error: {exc}"
        self._ro_text.config(state="normal")
        self._ro_text.delete("1.0", "end")
        self._ro_text.insert("1.0", content)
        self._ro_text.config(state="disabled")

    def _build_edit_view(self, parent: tk.Misc) -> ttk.Frame:
        view = ttk.Frame(parent)

        # Pack the actions bar first, pinned to the bottom, so it stays visible
        # no matter how tall the (expanding) editor grows.
        actions = ttk.Frame(view)
        actions.pack(side="bottom", fill="x", pady=(6, 0))
        self._validate_btn = ttk.Button(actions, text="Validate", command=self._validate)
        self._save_btn = ttk.Button(actions, text="Save", command=self._save)
        self._reload_btn = ttk.Button(actions, text="Reload from disk", command=self._reload)
        self._validate_btn.pack(side="left", padx=(0, 6))
        self._save_btn.pack(side="left", padx=(0, 6))
        self._reload_btn.pack(side="left")
        self._dirty_label = ttk.Label(actions, text="")
        self._dirty_label.pack(side="right")

        self._text = tk.Text(view, wrap="none", height=22, font=("Menlo", 11), undo=True)
        self._text.pack(side="top", fill="both", expand=True)
        self._text.bind("<<Modified>>", self._on_text_modified)
        return view

    def _on_text_modified(self, _event: object = None) -> None:
        # <<Modified>> fires whenever the buffer's modified flag flips; mirror it
        # into the unsaved-changes indicator. (_load_file / _persist reset it.)
        self._set_dirty(bool(self._text.edit_modified()))

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty_label.config(
            text="● unsaved changes" if dirty else "",
            foreground="#b26a00",
        )

    # --- mode / refresh -------------------------------------------------------

    def _set_view(self, view: str) -> None:
        """Swap the body to one of the service list / read-only config / editor."""
        self._view = view
        for v in (self._service_view, self._readonly_view, self._edit_view):
            v.pack_forget()
        if view == "services":
            self._service_view.pack(fill="both", expand=True)
            self._toggle_btn.config(text="View Config", command=self._show_readonly)
            self._banner.config(text="")
        elif view == "readonly":
            self._load_readonly()  # reflect the current on-disk config
            self._readonly_view.pack(fill="both", expand=True)
            self._toggle_btn.config(text="Back to Services", command=self._show_services)
            self._banner.config(
                text="Viewing configuration (read-only) — services still running."
            )
        else:  # edit
            self._load_file()  # reflect any on-disk changes when entering edit mode
            self._edit_view.pack(fill="both", expand=True)
            self._toggle_btn.config(text="Start All", command=self._start_all)
            self._banner.config(
                text="Editing configuration — Start All to validate, save, and launch."
            )
        # Logs/Routes are meaningful only while services run (i.e. not in edit mode).
        self._app.set_editing(view == "edit")

    def refresh(self, reschedule: bool = True) -> None:
        running = self._app.running
        # A change in run-state (incl. the first refresh) picks the natural view:
        # running -> the service list, stopped -> the editor. The read-only detour
        # is reachable only by the user and never survives a run-state change.
        if running != self._last_running:
            self._set_view("services" if running else "edit")
            self._last_running = running

        if self._view == "services":
            self._sync_tree(self._app.service_manager)
            self._update_service_controls()
        elif self._view == "edit":
            self._text.config(state="normal")
            for btn in (self._validate_btn, self._save_btn):
                btn.state(["!disabled"])

        if reschedule:
            self.after(_POLL_MS, self.refresh)

    def _show_readonly(self) -> None:
        self._set_view("readonly")

    def _show_services(self) -> None:
        self._set_view("services")

    def _sync_tree(self, sm: ServiceManager | None) -> None:
        """Update tree rows in place (keyed by service name) so the user's
        selection survives the periodic refresh instead of being cleared."""
        wanted: dict[str, tuple[object, ...]] = {}
        if sm is not None:
            for svc in sm.get_status():
                wanted[str(svc["name"])] = (
                    svc["status"],
                    svc["pid"] if svc["pid"] is not None else "-",
                    svc["restart_count"],
                    svc["exit_code"] if svc["exit_code"] is not None else "-",
                )
        for iid in self._tree.get_children():
            if iid not in wanted:
                self._tree.delete(iid)
        for name, values in wanted.items():
            if self._tree.exists(name):
                self._tree.item(name, values=values)
            else:
                self._tree.insert("", "end", iid=name, text=name, values=values)

    # --- service view actions -------------------------------------------------

    def _selected(self) -> str | None:
        sel = self._tree.selection()
        return sel[0] if sel else None

    def _act(self, method: str) -> None:
        sm = self._app.service_manager
        name = self._selected()
        if sm is None or not self._app.running:
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

    # --- lifecycle toggle -----------------------------------------------------

    def _stop_to_edit(self) -> None:
        self._status.config(text="stopping…", foreground="#000")
        self._app.stop_services()
        self._status.config(text="stopped — editing", foreground="#000")
        # running is now False, so refresh() switches to the editor (loading the file).
        self.refresh(reschedule=False)

    def _start_all(self) -> None:
        # Start always saves; save always validates (see _persist / _validate).
        if not self._persist():
            return
        self._status.config(text="starting…", foreground="#000")
        try:
            self._app.start_services()
        except Exception as exc:  # config may be invalid at start time
            self._status.config(text=f"start failed: {exc}", foreground="#b00")
            return
        self._status.config(text="saved & started ✓", foreground="#070")
        self.refresh(reschedule=False)

    # --- config editor --------------------------------------------------------

    def _load_file(self) -> None:
        try:
            content = self._app.paths.services_file.read_text()
        except OSError as exc:
            content = ""
            self._status.config(text=f"read error: {exc}", foreground="#b00")
        self._text.config(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("1.0", content)
        self._text.edit_modified(False)  # freshly loaded buffer matches disk

    def _reload(self) -> None:
        self._load_file()
        self._status.config(text="reloaded from disk", foreground="#000")

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
        dest = self._app.paths.services_file
        try:
            # Write to a temp sibling then atomically replace, so a failed write
            # never leaves services.toml half-written / corrupted.
            tmp = dest.with_name(f"{dest.name}.tmp")
            tmp.write_text(text)
            os.replace(tmp, dest)
        except OSError as exc:
            self._status.config(text=f"write error: {exc}", foreground="#b00")
            return False
        self._text.edit_modified(False)  # buffer now matches disk
        return True

    def _save(self) -> None:
        if self._persist():
            self._status.config(text="saved ✓", foreground="#070")


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
        current = self._service.get()
        if current not in names:
            # Selection was removed (or none yet): pick the first, else clear.
            if names:
                self._service.current(0)
            else:
                self._service.set("")

    def _tick(self) -> None:
        sm = self._app.service_manager
        # Refresh the list each tick so added/removed services show up live.
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

        ttk.Label(
            self,
            text="Routes grouped by service — each service's URLs and the port they proxy to. "
            "Double-click a URL to open it.",
        ).pack(anchor="w")
        self._banner = ttk.Label(self, text="", foreground="#b00")
        self._banner.pack(fill="x")

        self._tree = ttk.Treeview(self, columns=("url", "target"), show="tree headings", height=12)
        self._tree.heading("#0", text="service / route")
        self._tree.heading("url", text="url")
        self._tree.heading("target", text="→ proxies to")
        self._tree.column("#0", width=220, anchor="w")
        self._tree.column("url", width=300, anchor="w")
        self._tree.column("target", width=150, anchor="w")
        self._tree.tag_configure("service", font=("TkDefaultFont", 11, "bold"))
        self._tree.tag_configure("muted", foreground="#999")
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
            self._add_service(service, manifest.http_port)

    def _add_service(self, service: object, http_port: int) -> None:
        # A service (parent) with its routes nested beneath, so the
        # service ↔ route relationship is visible at a glance.
        if service.disabled:  # type: ignore[attr-defined]
            note, tags = "  (disabled — routes inactive)", ("service", "muted")
        elif service.command is None:  # type: ignore[attr-defined]
            note, tags = "  (external — not started here)", ("service",)
        else:
            note, tags = "", ("service",)
        parent = self._tree.insert(
            "", "end", text=f"{service.name}{note}",  # type: ignore[attr-defined]
            values=("", ""), open=True, tags=tags,
        )
        if service.disabled:  # type: ignore[attr-defined]
            # Disabled services' routes are excluded from the proxy, so don't show
            # (openable) route rows for them — just the disabled parent above.
            return
        for route in service.routes:  # type: ignore[attr-defined]
            target = self._target(service, route)
            for host in route.hosts:
                if "*" in host:
                    self._tree.insert(parent, "end", text=route.id,
                                      values=(f"{host}  (wildcard)", target), tags=("muted",))
                else:
                    url = f"http://{host}:{http_port}/"
                    self._tree.insert(parent, "end", text=route.id, values=(url, target))

    @staticmethod
    def _target(service: object, route: object) -> str:
        host = route.target_host  # type: ignore[attr-defined]
        if route.target_port is not None:  # type: ignore[attr-defined]
            return f"{host}:{route.target_port}"  # type: ignore[attr-defined]
        env_name = route.target_port_env  # type: ignore[attr-defined]
        port = service.env.get(env_name)  # type: ignore[attr-defined]
        return f"{host}:{port}" if port else f"{host}:${{{env_name}}}"

    def _open(self, _event: tk.Event) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        values = self._tree.item(sel[0], "values")
        url = values[0] if values else ""
        if url.startswith("http://"):  # skip service parents and wildcard rows
            webbrowser.open(url)


def run_gui() -> None:
    paths = ensure_config()
    lock_fd = _acquire_lock()

    root = tk.Tk()
    root.title("Local Dev Proxy — Manager")
    root.geometry("760x560")

    app = ManagerApp(root, paths, lock_fd)
    # Start services before building the window so the Services tab paints in its
    # running (list) view from the first frame — no flash of the config editor.
    # start_services() rolls back on failure (running stays False), so a bad
    # config simply opens the tab in edit mode; the window comes up either way.
    try:
        app.start_services()
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the window
        print(f"Startup failed; services left stopped: {exc}", file=sys.stderr)
    app.build_ui()
    app.install_icon()
    app.wire_lifecycle()
    app.install_signals()

    try:
        root.mainloop()
    finally:
        # Guarantee teardown even if mainloop exits abnormally (e.g. an
        # unhandled exception in a Tk callback) so services never outlive the app.
        app.quit()
