from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import httpx

_routes_lock = threading.Lock()


class CaddyAPIError(RuntimeError):
    pass


class CaddyAdminClient:
    def __init__(self, admin_url: str, timeout_seconds: float = 5.0) -> None:
        self.admin_url = admin_url.rstrip("/")
        self._client = httpx.Client(base_url=self.admin_url, timeout=timeout_seconds)

    def __enter__(self) -> "CaddyAdminClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def healthcheck(self) -> None:
        response = self._request("GET", "/config/")
        self._ensure_success(response, "Caddy admin healthcheck failed")

    def ensure_server(self, bootstrap_config_path: Path) -> None:
        self.healthcheck()

        response = self._request("GET", "/config/apps/http/servers/srv0")
        if response.status_code == 200:
            return

        if response.status_code == 404:
            self.load_bootstrap(bootstrap_config_path)
            return

        self._ensure_success(response, "Failed to inspect Caddy srv0 server")

    def load_bootstrap(self, bootstrap_config_path: Path) -> None:
        if not bootstrap_config_path.exists():
            raise CaddyAPIError(f"Bootstrap Caddy config does not exist: {bootstrap_config_path}")

        payload = json.loads(bootstrap_config_path.read_text())
        response = self._request(
            "POST",
            "/load",
            headers={"Content-Type": "application/json"},
            content=json.dumps(payload),
        )
        self._ensure_success(response, "Failed to load bootstrap Caddy config")

    def set_routes(self, routes: list[dict]) -> None:
        path = "/config/apps/http/servers/srv0/routes"
        with _routes_lock:
            self._request("DELETE", path)
            response = self._request("PUT", path, json=routes)
        self._ensure_success(response, "Failed to update Caddy routes")

    def get_routes(self) -> list[dict]:
        response = self._request("GET", "/config/apps/http/servers/srv0/routes")
        if response.status_code == 404:
            return []

        self._ensure_success(response, "Failed to fetch Caddy routes")
        payload = response.json()
        if not isinstance(payload, list):
            raise CaddyAPIError("Unexpected routes payload from Caddy admin API")
        return payload

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise CaddyAPIError(f"Unable to reach Caddy admin API at {self.admin_url}: {exc}") from exc

    @staticmethod
    def _ensure_success(response: httpx.Response, context: str) -> None:
        if 200 <= response.status_code < 300:
            return

        body = response.text.strip()
        detail = body if body else "<empty body>"
        raise CaddyAPIError(
            f"{context}: HTTP {response.status_code} from {response.request.url} - {detail}"
        )
