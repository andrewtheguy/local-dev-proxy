from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .routes import RoutesManifest, resolve_command

_PIDFILE_NAME = "service_pids.json"

logger = logging.getLogger(__name__)


@dataclass
class ServiceInfo:
    name: str
    command: list[str] | None
    env: dict[str, str]
    managed: bool = True
    status: str = "stopped"  # running | stopped | crashed | unmanaged
    pid: int | None = None
    exit_code: int | None = None
    restart_count: int = 0
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)


class ServiceManager:
    def __init__(self, manifest: RoutesManifest, log_dir: Path, cwd: Path) -> None:
        self._lock = threading.Lock()
        self._services: dict[str, ServiceInfo] = {}
        self._log_dir = log_dir
        self._cwd = cwd
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None

        self._log_dir.mkdir(parents=True, exist_ok=True)

        for name, service_def in manifest.services.items():
            if service_def.command is None:
                self._services[name] = ServiceInfo(
                    name=name,
                    command=None,
                    env={},
                    managed=False,
                    status="unmanaged",
                )
            else:
                effective_env = {**service_def.env, **os.environ}
                command = resolve_command(service_def.command, effective_env)
                runtime_env = {**os.environ, **service_def.env}
                self._services[name] = ServiceInfo(
                    name=name,
                    command=command,
                    env=runtime_env,
                )

    def start_all(self) -> None:
        self._kill_stale_pids()
        with self._lock:
            for name, info in self._services.items():
                if info.managed:
                    self._start_service_locked(name)
            self._save_pidfile()
        self._start_monitor()

    def stop_all(self) -> None:
        self._stop_monitor()
        with self._lock:
            for name, info in self._services.items():
                if info.managed:
                    self._stop_service_locked(name)
            self._remove_pidfile()

    def start_service(self, name: str) -> None:
        with self._lock:
            self._require_managed(name)
            self._start_service_locked(name)

    def stop_service(self, name: str) -> None:
        with self._lock:
            self._require_managed(name)
            self._stop_service_locked(name)

    def restart_service(self, name: str) -> None:
        with self._lock:
            self._require_managed(name)
            self._stop_service_locked(name)
            info = self._services[name]
            info.restart_count += 1
            self._start_service_locked(name)

    def get_status(self) -> list[dict[str, object]]:
        with self._lock:
            result = []
            for info in self._services.values():
                result.append({
                    "name": info.name,
                    "status": info.status,
                    "managed": info.managed,
                    "pid": info.pid,
                    "exit_code": info.exit_code,
                    "restart_count": info.restart_count,
                })
            return result

    def get_log_path(self, name: str) -> Path:
        return self._log_dir / f"{name}.log"

    def service_names(self) -> list[str]:
        return list(self._services.keys())

    def _require_service(self, name: str) -> None:
        if name not in self._services:
            raise KeyError(f"Unknown service: {name}")

    def _require_managed(self, name: str) -> None:
        self._require_service(name)
        if not self._services[name].managed:
            raise KeyError(f"Service '{name}' is unmanaged")

    def _start_service_locked(self, name: str) -> None:
        info = self._services[name]
        assert info.command is not None, f"Cannot start unmanaged service: {name}"
        if info.status == "running" and info.process and info.process.poll() is None:
            return

        log_path = self.get_log_path(name)
        log_file = open(log_path, "a")
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        log_file.write(f"\n{'='*60}\n")
        log_file.write(f"--- {name} started at {timestamp} ---\n")
        log_file.write(f"{'='*60}\n")
        log_file.flush()

        try:
            process = subprocess.Popen(
                info.command,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=info.env,
                cwd=self._cwd,
                start_new_session=True,
            )
        except FileNotFoundError:
            log_file.write(f"ERROR: Command not found: {info.command[0]}\n")
            log_file.close()
            info.status = "crashed"
            info.pid = None
            logger.error("Failed to start %s: command not found: %s", name, info.command[0])
            return

        info.process = process
        info.pid = process.pid
        info.status = "running"
        info.exit_code = None
        self._save_pidfile()
        logger.info("Started %s (PID %d)", name, process.pid)

    def _stop_service_locked(self, name: str) -> None:
        info = self._services[name]
        process = info.process
        if process is None or process.poll() is not None:
            info.status = "stopped"
            info.pid = None
            info.process = None
            return

        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait(timeout=3)

        info.exit_code = process.returncode
        info.status = "stopped"
        info.pid = None
        info.process = None
        logger.info("Stopped %s (exit code %s)", name, info.exit_code)

    @property
    def _pidfile_path(self) -> Path:
        return self._log_dir / _PIDFILE_NAME

    def _save_pidfile(self) -> None:
        pids: dict[str, int] = {}
        for info in self._services.values():
            if info.pid is not None:
                pids[info.name] = info.pid
        try:
            self._pidfile_path.write_text(json.dumps(pids))
        except OSError:
            pass

    def _remove_pidfile(self) -> None:
        try:
            self._pidfile_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _kill_stale_pids(self) -> None:
        try:
            data = json.loads(self._pidfile_path.read_text())
        except (OSError, json.JSONDecodeError):
            return

        for name, pid in data.items():
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                logger.info("Killed stale process group for %s (PID %d)", name, pid)
            except (ProcessLookupError, PermissionError, OSError):
                pass

        self._remove_pidfile()

    def _start_monitor(self) -> None:
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def _stop_monitor(self) -> None:
        self._monitor_stop.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5)

    def _monitor_loop(self) -> None:
        while not self._monitor_stop.wait(timeout=2):
            with self._lock:
                for info in self._services.values():
                    if info.status != "running" or info.process is None:
                        continue
                    returncode = info.process.poll()
                    if returncode is not None:
                        info.exit_code = returncode
                        info.status = "crashed"
                        info.pid = None
                        info.process = None
                        logger.warning(
                            "Service %s crashed (exit code %d)", info.name, returncode
                        )
