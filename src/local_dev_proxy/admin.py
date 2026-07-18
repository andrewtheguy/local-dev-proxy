from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import manager_pid, manager_running
from .proxy import ADMIN_PORT

__all__ = [
    "AdminError",
    "ServiceStatus",
    "manager_pid",
    "manager_running",
    "list_services",
    "start_service",
    "stop_service",
    "restart_service",
    "service_logs",
]


class AdminError(RuntimeError):
    """Raised when the admin API cannot be reached or returns an error."""


@dataclass(frozen=True)
class ServiceStatus:
    name: str
    status: str
    pid: int | None
    restart_count: int
    exit_code: int | None


def _request(method: str, path: str) -> str:
    url = f"http://127.0.0.1:{ADMIN_PORT}{path}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode()
    except urllib.error.URLError as exc:
        raise AdminError(f"could not reach admin API: {exc}") from exc


def list_services() -> list[ServiceStatus]:
    body = _request("GET", "/services")
    return [
        ServiceStatus(
            name=svc["name"],
            status=svc["status"],
            pid=svc.get("pid"),
            restart_count=svc.get("restart_count", 0),
            exit_code=svc.get("exit_code"),
        )
        for svc in json.loads(body)
    ]


def start_service(name: str) -> None:
    _request("POST", f"/services/{name}/start")


def stop_service(name: str) -> None:
    _request("POST", f"/services/{name}/stop")


def restart_service(name: str) -> None:
    _request("POST", f"/services/{name}/restart")


def service_logs(name: str, lines: int = 100) -> str:
    return _request("GET", f"/services/{name}/logs?lines={lines}")
