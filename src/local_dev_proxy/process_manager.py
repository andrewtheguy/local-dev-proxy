from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from .log_rotation import (
    LOG_BACKUP_COUNT,
    LOG_MAX_BYTES,
    RotatingLogWriter,
    pump_log_stream,
)
from .routes import RoutesManifest, resolve_command

logger = logging.getLogger(__name__)


def _request_process_tree_stop(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except OSError:
            # GUI builds may not own a console, so a console control event cannot
            # always be delivered. Fall back to terminating the complete tree.
            _force_process_tree_stop(process)
        return

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def _force_process_tree_stop(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if process.poll() is None:
            process.kill()
        return

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


@dataclass
class ServiceInfo:
    name: str
    command: list[str] | None
    env: dict[str, str]
    managed: bool = True
    status: str = "stopped"  # running | stopped | crashed | unmanaged | disabled
    pid: int | None = None
    exit_code: int | None = None
    restart_count: int = 0
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    log_thread: threading.Thread | None = field(default=None, repr=False)
    log_stop_event: threading.Event | None = field(default=None, repr=False)


class ServiceManager:
    def __init__(
        self,
        manifest: RoutesManifest,
        log_dir: Path,
        cwd: Path,
        *,
        log_max_bytes: int = LOG_MAX_BYTES,
        log_backup_count: int = LOG_BACKUP_COUNT,
    ) -> None:
        self._lock = threading.Lock()
        self._services: dict[str, ServiceInfo] = {}
        self._log_dir = log_dir
        self._cwd = cwd
        self._log_max_bytes = log_max_bytes
        self._log_backup_count = log_backup_count
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None

        self._log_dir.mkdir(parents=True, exist_ok=True)

        for name, service_def in manifest.services.items():
            if service_def.disabled:
                self._services[name] = ServiceInfo(
                    name=name,
                    command=None,
                    env={},
                    managed=False,
                    status="disabled",
                )
            elif service_def.command is None:
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
        with self._lock:
            for name, info in self._services.items():
                if info.managed:
                    self._start_service_locked(name)
        self._start_monitor()

    def stop_all(self) -> None:
        self._stop_monitor()
        with self._lock:
            for name, info in self._services.items():
                if info.managed:
                    # Isolate each stop so one child's teardown failure doesn't
                    # leave the remaining managed processes running.
                    try:
                        self._stop_service_locked(name)
                    except Exception:
                        logger.exception("Error stopping service %s", name)

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
            self._start_service_locked(name)
            info.restart_count += 1

    def get_status(self) -> list[dict[str, object]]:
        with self._lock:
            result = []
            for info in self._services.values():
                result.append(
                    {
                        "name": info.name,
                        "status": info.status,
                        "managed": info.managed,
                        "pid": info.pid,
                        "exit_code": info.exit_code,
                        "restart_count": info.restart_count,
                    }
                )
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
        if info.log_thread is not None:
            if info.log_thread.is_alive():
                raise RuntimeError(
                    f"Cannot start {name}: previous log capture is still active"
                )
            info.log_thread = None
            info.log_stop_event = None

        log_path = self.get_log_path(name)
        log_writer = RotatingLogWriter(
            log_path,
            max_bytes=self._log_max_bytes,
            backup_count=self._log_backup_count,
        )
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        log_writer.write(
            (
                f"\n{'=' * 60}\n"
                f"--- {name} started at {timestamp} ---\n"
                f"{'=' * 60}\n"
            ).encode()
        )

        try:
            process = subprocess.Popen(
                info.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=info.env,
                cwd=self._cwd,
                start_new_session=os.name != "nt",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )
        except FileNotFoundError:
            log_writer.write(f"ERROR: Command not found: {info.command[0]}\n".encode())
            log_writer.close()
            info.status = "crashed"
            info.pid = None
            logger.error(
                "Failed to start %s: command not found: %s", name, info.command[0]
            )
            return
        except BaseException:
            log_writer.close()
            raise

        assert process.stdout is not None
        log_stop_event = threading.Event()
        log_thread = threading.Thread(
            target=self._pump_service_log,
            args=(process.stdout, log_writer, log_stop_event),
            name=f"{name}-log-writer",
            daemon=True,
        )
        info.process = process
        info.log_thread = log_thread
        info.log_stop_event = log_stop_event
        info.pid = process.pid
        info.status = "running"
        info.exit_code = None
        log_thread.start()
        logger.info("Started %s (PID %d)", name, process.pid)

    @staticmethod
    def _pump_service_log(
        source: BinaryIO,
        writer: RotatingLogWriter,
        stop_event: threading.Event,
    ) -> None:
        pump_log_stream(source, writer, stop_event)

    @staticmethod
    def _finish_log_capture(info: ServiceInfo) -> None:
        thread = info.log_thread
        if thread is None:
            return
        thread.join(timeout=5)
        if thread.is_alive():
            if info.log_stop_event is not None:
                info.log_stop_event.set()
            thread.join(timeout=1)
        if thread.is_alive():
            logger.warning("Log writer for service %s did not stop", info.name)
            return
        info.log_thread = None
        info.log_stop_event = None

    def _stop_service_locked(self, name: str) -> None:
        info = self._services[name]
        process = info.process
        if process is None or process.poll() is not None:
            self._finish_log_capture(info)
            info.status = "stopped"
            info.pid = None
            info.process = None
            return

        _request_process_tree_stop(process)

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _force_process_tree_stop(process)
            process.wait(timeout=3)

        self._finish_log_capture(info)
        info.exit_code = process.returncode
        info.status = "stopped"
        info.pid = None
        info.process = None
        logger.info("Stopped %s (exit code %s)", name, info.exit_code)

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
                        self._finish_log_capture(info)
                        info.exit_code = returncode
                        info.status = "crashed"
                        info.pid = None
                        info.process = None
                        logger.warning(
                            "Service %s crashed (exit code %d)", info.name, returncode
                        )
