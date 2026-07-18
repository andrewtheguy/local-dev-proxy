from __future__ import annotations

import os
import signal
import time
import tkinter as tk
import webbrowser
from tkinter import ttk

from . import admin
from .config import ProjectPaths, ensure_config, manager_pid, manager_running
from .routes import RouteConfigError, load_routes, validate_toml

_POLL_MS = 2000


def _stop_manager() -> bool:
    """SIGTERM the running manager and wait for it to exit. Returns True if stopped."""
    pid = manager_pid()
    if pid is None:
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    for _ in range(50):
        if manager_pid() is None:
            return True
        time.sleep(0.1)
    return False


def _start_manager_detached() -> None:
    from .cli import _spawn_detached

    _spawn_detached()


class ServicesTab(ttk.Frame):
    COLUMNS = ("status", "pid", "restarts", "exit")

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=8)

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
        self._start_btn = ttk.Button(buttons, text="Start", command=lambda: self._act(admin.start_service))
        self._stop_btn = ttk.Button(buttons, text="Stop", command=lambda: self._act(admin.stop_service))
        self._restart_btn = ttk.Button(buttons, text="Restart", command=lambda: self._act(admin.restart_service))
        self._start_mgr_btn = ttk.Button(buttons, text="Start Manager", command=self._start_manager)
        for btn in (self._start_btn, self._stop_btn, self._restart_btn):
            btn.pack(side="left", padx=(0, 6))
        self._start_mgr_btn.pack(side="right")

        self.refresh()

    def _selected(self) -> str | None:
        sel = self._tree.selection()
        return self._tree.item(sel[0], "text") if sel else None

    def _act(self, fn) -> None:
        name = self._selected()
        if not name:
            self._banner.config(text="Select a service first.")
            return
        try:
            fn(name)
            self._banner.config(text="")
        except admin.AdminError as exc:
            self._banner.config(text=f"Error: {exc}")
        self.refresh(reschedule=False)

    def _start_manager(self) -> None:
        _start_manager_detached()
        self._banner.config(text="Starting manager…")

    def refresh(self, reschedule: bool = True) -> None:
        running = manager_running()
        self._start_mgr_btn.state(["disabled"] if running else ["!disabled"])
        for btn in (self._start_btn, self._stop_btn, self._restart_btn):
            btn.state(["!disabled"] if running else ["disabled"])

        self._tree.delete(*self._tree.get_children())
        if not running:
            self._banner.config(text="Manager is not running.")
        else:
            try:
                for svc in admin.list_services():
                    self._tree.insert(
                        "", "end", text=svc.name,
                        values=(
                            svc.status,
                            svc.pid if svc.pid is not None else "-",
                            svc.restart_count,
                            svc.exit_code if svc.exit_code is not None else "-",
                        ),
                    )
                if self._banner.cget("text") in ("Manager is not running.", ""):
                    self._banner.config(text="")
            except admin.AdminError as exc:
                self._banner.config(text=f"Error: {exc}")

        if reschedule:
            self.after(_POLL_MS, self.refresh)


class LogsTab(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=8)

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

        self._text = tk.Text(self, wrap="none", height=20, font=("Menlo", 11))
        self._text.pack(fill="both", expand=True, pady=(6, 0))
        yscroll = ttk.Scrollbar(self._text, command=self._text.yview)
        yscroll.pack(side="right", fill="y")
        self._text.config(yscrollcommand=yscroll.set, state="disabled")

        self._populate_services()
        self._tick()

    def _populate_services(self) -> None:
        try:
            names = [svc.name for svc in admin.list_services()]
        except admin.AdminError:
            names = []
        self._service["values"] = names
        if names and not self._service.get():
            self._service.current(0)

    def _tick(self) -> None:
        name = self._service.get()
        if not name:
            self._populate_services()
            name = self._service.get()
        if name:
            try:
                lines = int(self._lines.get())
            except ValueError:
                lines = 200
            try:
                body = admin.service_logs(name, lines)
            except admin.AdminError as exc:
                body = f"[error] {exc}"
            self._set_text(body)

        if self._follow.get():
            self.after(_POLL_MS, self._tick)

    def _set_text(self, body: str) -> None:
        at_bottom = self._text.yview()[1] >= 0.999
        self._text.config(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("1.0", body)
        self._text.config(state="disabled")
        if at_bottom:
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
    def __init__(self, master: tk.Misc, paths: ProjectPaths) -> None:
        super().__init__(master, padding=8)
        self._paths = paths

        self._banner = ttk.Label(self, text="", foreground="#b00")
        self._banner.pack(fill="x")

        self._stop_btn = ttk.Button(self, text="Stop Manager to Edit", command=self._stop)
        self._stop_btn.pack(anchor="w", pady=(0, 4))

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
            content = self._paths.services_file.read_text()
        except OSError as exc:
            content = ""
            self._status.config(text=f"read error: {exc}")
        self._text.delete("1.0", "end")
        self._text.insert("1.0", content)

    def refresh(self) -> None:
        running = manager_running()
        if running:
            self._banner.config(text="Manager is running — stop it to edit the configuration.")
            self._text.config(state="disabled")
            self._stop_btn.state(["!disabled"])
            for btn in (self._validate_btn, self._save_btn):
                btn.state(["disabled"])
        else:
            self._banner.config(text="Manager is stopped — edits apply on the next start.")
            self._text.config(state="normal")
            self._stop_btn.state(["disabled"])
            for btn in (self._validate_btn, self._save_btn):
                btn.state(["!disabled"])
        self.after(_POLL_MS, self.refresh)

    def _stop(self) -> None:
        self._status.config(text="stopping…")
        if _stop_manager():
            self._status.config(text="manager stopped")
        else:
            self._status.config(text="manager did not stop")
        self.refresh_now()

    def refresh_now(self) -> None:
        # one-shot state refresh without waiting for the poll
        running = manager_running()
        self._text.config(state="disabled" if running else "normal")

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

    def _save(self) -> None:
        if manager_running():
            self._status.config(text="stop the manager first", foreground="#b00")
            return
        if not self._validate():
            return
        text = self._text.get("1.0", "end-1c")
        try:
            self._paths.services_file.write_text(text)
        except OSError as exc:
            self._status.config(text=f"write error: {exc}", foreground="#b00")
            return
        self._status.config(text="saved ✓", foreground="#070")


def run_gui() -> None:
    paths = ensure_config()

    root = tk.Tk()
    root.title("Local Dev Proxy — Manager")
    root.geometry("720x520")

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)

    notebook.add(ServicesTab(notebook), text="Services")
    notebook.add(LogsTab(notebook), text="Logs")
    notebook.add(RoutesTab(notebook, paths), text="Routes")
    notebook.add(ConfigTab(notebook, paths), text="Config")

    root.mainloop()
