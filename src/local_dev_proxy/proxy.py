from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from fnmatch import fnmatch
import html
import logging
import os
import threading
from pathlib import Path
from typing import Mapping

import json

import aiohttp
from aiohttp import web

from .config import require_port
from .routes import RouteConfigError, RoutesManifest, load_routes

logger = logging.getLogger(__name__)

ADMIN_PORT = 24019


def _configure_logging() -> None:
    pkg_logger = logging.getLogger("local_dev_proxy")
    if not pkg_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
        pkg_logger.addHandler(handler)
        pkg_logger.setLevel(logging.INFO)


@dataclass(frozen=True)
class ResolvedRoute:
    id: str
    host_patterns: tuple[str, ...]
    target_host: str
    target_port: int


def resolve_routes(
    manifest: RoutesManifest, env: Mapping[str, str] | None = None,
) -> list[ResolvedRoute]:
    resolved: list[ResolvedRoute] = []
    seen_ports: dict[int, str] = {}

    for service in manifest.services.values():
        effective_env: dict[str, str] = dict(service.env)
        if env:
            effective_env.update(env)

        for route in service.routes:
            if route.target_port is not None:
                port = route.target_port
            else:
                assert route.target_port_env is not None
                port = require_port(effective_env, route.target_port_env)

            if port in seen_ports:
                raise RouteConfigError(
                    f"Port {port} used by both {seen_ports[port]} and {route.id}"
                )
            seen_ports[port] = route.id

            resolved.append(
                ResolvedRoute(
                    id=route.id,
                    host_patterns=route.hosts,
                    target_host=route.target_host,
                    target_port=port,
                )
            )

    return resolved


class RouteTable:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._routes: list[ResolvedRoute] = []
        self._portal_html: str = ""

    @property
    def routes(self) -> list[ResolvedRoute]:
        with self._lock:
            return list(self._routes)

    @property
    def portal_html(self) -> str:
        with self._lock:
            return self._portal_html

    def reload(self, services_file: Path, env: Mapping[str, str] | None = None) -> None:
        manifest = load_routes(services_file)
        routes = resolve_routes(manifest, env)
        portal = _build_portal_html(manifest, routes)
        with self._lock:
            self._routes = routes
            self._portal_html = portal

    def match(self, host: str) -> ResolvedRoute | None:
        with self._lock:
            for route in self._routes:
                for pattern in route.host_patterns:
                    if fnmatch(host, pattern):
                        return route
        return None


def _build_portal_html(manifest: RoutesManifest, routes: list[ResolvedRoute]) -> str:
    http_port = manifest.http_port
    items: list[str] = []
    for route in routes:
        for host in route.host_patterns:
            if "*" in host:
                items.append(f"<li>{html.escape(host)}</li>")
            else:
                url = f"http://{host}:{http_port}/"
                items.append(
                    f'<li><a href="{html.escape(url)}">'
                    f"{html.escape(host)}</a></li>"
                )

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Local Dev Proxy</title></head><body>"
        "<h1>Local Dev Proxy</h1><ul>"
        + "\n".join(items)
        + "</ul></body></html>"
    )


# --- Proxy handler ---

HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})

WEBSOCKET_HANDSHAKE_HEADERS = frozenset({
    "sec-websocket-extensions",
    "sec-websocket-key",
    "sec-websocket-protocol",
    "sec-websocket-version",
})


def _filter_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


def _filter_websocket_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        k: v for k, v in headers.items()
        if k.lower() not in HOP_BY_HOP
        and k.lower() not in WEBSOCKET_HANDSHAKE_HEADERS
        and k.lower() != "origin"
    }


def _get_websocket_protocols(request: web.Request) -> tuple[str, ...]:
    raw_protocols = request.headers.get("Sec-WebSocket-Protocol", "")
    return tuple(
        protocol.strip()
        for protocol in raw_protocols.split(",")
        if protocol.strip()
    )


def _get_websocket_close_args(msg: aiohttp.WSMessage) -> tuple[int, str | bytes]:
    code = msg.data if isinstance(msg.data, int) else aiohttp.WSCloseCode.OK
    message = msg.extra if msg.extra is not None else b""
    return code, message


def _get_websocket_error_detail(msg: aiohttp.WSMessage) -> object:
    return msg.data if msg.data is not None else msg.extra


def make_proxy_app(route_table: RouteTable) -> web.Application:
    app = web.Application(client_max_size=100 * 1024 * 1024)  # 100MB
    app["route_table"] = route_table
    app.router.add_route("*", "/{path_info:.*}", _proxy_handler)
    return app


async def _proxy_handler(request: web.Request) -> web.StreamResponse:
    route_table: RouteTable = request.app["route_table"]
    raw_host = request.host
    # Strip port from Host header
    host = raw_host.rsplit(":", 1)[0] if ":" in raw_host else raw_host

    # Portal page for localhost
    if host == "localhost":
        logger.info("%s %s %s -> portal", request.method, host, request.path)
        return web.Response(
            text=route_table.portal_html,
            content_type="text/html",
            charset="utf-8",
        )

    route = route_table.match(host)
    if route is None:
        logger.info("%s %s %s -> 404", request.method, host, request.path)
        return web.Response(status=404, text="Not Found")

    host_part = f"[{route.target_host}]" if ":" in route.target_host else route.target_host
    target_base = f"http://{host_part}:{route.target_port}"
    path = request.match_info.get("path_info", "")
    target_url = f"{target_base}/{path}"
    if request.query_string:
        target_url += f"?{request.query_string}"

    # WebSocket upgrade
    if _is_websocket_upgrade(request):
        logger.info("%s %s %s -> ws %s", request.method, host, request.path, route.id)
        return await _proxy_websocket(request, target_url)

    resp = await _proxy_http(request, target_url)
    logger.info("%s %s %s -> %s %d", request.method, host, request.path, route.id, resp.status)
    return resp


def _is_websocket_upgrade(request: web.Request) -> bool:
    upgrade = request.headers.get("Upgrade", "").lower()
    return upgrade == "websocket"


async def _proxy_http(request: web.Request, target_url: str) -> web.StreamResponse:
    headers = _filter_headers(request.headers)
    body = await request.read()

    async with aiohttp.ClientSession() as session:
        try:
            async with session.request(
                request.method,
                target_url,
                headers=headers,
                data=body,
                allow_redirects=False,
            ) as upstream:
                response_headers = _filter_headers(upstream.headers)
                resp = web.StreamResponse(
                    status=upstream.status,
                    headers=response_headers,
                )
                await resp.prepare(request)

                async for chunk in upstream.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except aiohttp.ClientError as exc:
            logger.debug("Upstream error for %s: %s", target_url, exc)
            return web.Response(status=502, text="Bad Gateway")


async def _proxy_websocket(request: web.Request, target_url: str) -> web.StreamResponse:
    ws_target = target_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    try:
        async with aiohttp.ClientSession() as session:
            ws_headers = _filter_websocket_headers(request.headers)
            ws_protocols = _get_websocket_protocols(request)
            async with session.ws_connect(
                ws_target,
                headers=ws_headers,
                origin=request.headers.get("Origin"),
                protocols=ws_protocols,
                autoclose=False,
            ) as ws_upstream:
                response_protocols = (ws_upstream.protocol,) if ws_upstream.protocol else ()
                ws_response = web.WebSocketResponse(protocols=response_protocols, autoclose=False)
                await ws_response.prepare(request)

                async def _forward_client_to_upstream() -> tuple[str, int, str | bytes]:
                    while True:
                        msg = await ws_response.receive()
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await ws_upstream.send_str(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await ws_upstream.send_bytes(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                            code, message = _get_websocket_close_args(msg)
                            return ("upstream", code, message)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.warning("WebSocket client error: %r", _get_websocket_error_detail(msg))
                            return ("both", aiohttp.WSCloseCode.INTERNAL_ERROR, b"")
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            return ("upstream", aiohttp.WSCloseCode.OK, b"")

                async def _forward_upstream_to_client() -> tuple[str, int, str | bytes]:
                    while True:
                        msg = await ws_upstream.receive()
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await ws_response.send_str(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await ws_response.send_bytes(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                            code, message = _get_websocket_close_args(msg)
                            return ("client", code, message)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.warning("WebSocket upstream error: %r", _get_websocket_error_detail(msg))
                            return ("both", aiohttp.WSCloseCode.INTERNAL_ERROR, b"")
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            return ("client", aiohttp.WSCloseCode.OK, b"")

                client_task = asyncio.create_task(_forward_client_to_upstream())
                upstream_task = asyncio.create_task(_forward_upstream_to_client())
                done, pending = await asyncio.wait(
                    {client_task, upstream_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                close_target, code, message = next(task.result() for task in done)

                for task in pending:
                    task.cancel()
                for task in pending:
                    with suppress(asyncio.CancelledError):
                        await task

                if close_target in ("upstream", "both"):
                    await ws_upstream.close(code=code, message=message)
                if close_target in ("client", "both"):
                    await ws_response.close(code=code, message=message)
                return ws_response
    except aiohttp.WSServerHandshakeError as exc:
        logger.debug("WebSocket upstream rejected handshake: %s", exc)
        return web.Response(status=exc.status, text=exc.message or "WebSocket handshake failed")
    except aiohttp.ClientError as exc:
        logger.debug("WebSocket upstream error: %s", exc)
        return web.Response(status=502, text="Bad Gateway")


# --- Admin handler ---

def make_admin_app(
    route_table: RouteTable,
    services_file: Path,
    service_manager: object | None = None,
) -> web.Application:
    from .process_manager import ServiceManager

    app = web.Application()
    app["route_table"] = route_table
    app["services_file"] = services_file
    if service_manager is not None:
        app["service_manager"] = service_manager
    app.router.add_get("/healthz", _healthz_handler)
    app.router.add_post("/reload", _reload_handler)

    if isinstance(service_manager, ServiceManager):
        app.router.add_get("/services", _services_status_handler)
        app.router.add_post("/services/{name}/restart", _service_restart_handler)
        app.router.add_post("/services/{name}/stop", _service_stop_handler)
        app.router.add_post("/services/{name}/start", _service_start_handler)
        app.router.add_get("/services/{name}/logs", _service_logs_handler)

    return app


async def _healthz_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _reload_handler(request: web.Request) -> web.Response:
    route_table: RouteTable = request.app["route_table"]
    services_file: Path = request.app["services_file"]
    try:
        route_table.reload(services_file, env=os.environ)
        ids = [r.id for r in route_table.routes]
        logger.info("Routes reloaded: %s", ", ".join(ids))
        return web.Response(text="reloaded")
    except Exception as exc:
        logger.exception("Reload failed")
        return web.Response(status=500, text=str(exc))


async def _services_status_handler(request: web.Request) -> web.Response:
    from .process_manager import ServiceManager
    sm: ServiceManager = request.app["service_manager"]
    return web.Response(
        text=json.dumps(sm.get_status()),
        content_type="application/json",
    )


async def _service_restart_handler(request: web.Request) -> web.Response:
    from .process_manager import ServiceManager
    sm: ServiceManager = request.app["service_manager"]
    name = request.match_info["name"]
    try:
        sm.restart_service(name)
        return web.Response(text=json.dumps({"ok": True}), content_type="application/json")
    except KeyError as exc:
        return web.Response(status=404, text=str(exc))


async def _service_stop_handler(request: web.Request) -> web.Response:
    from .process_manager import ServiceManager
    sm: ServiceManager = request.app["service_manager"]
    name = request.match_info["name"]
    try:
        sm.stop_service(name)
        return web.Response(text=json.dumps({"ok": True}), content_type="application/json")
    except KeyError as exc:
        return web.Response(status=404, text=str(exc))


async def _service_start_handler(request: web.Request) -> web.Response:
    from .process_manager import ServiceManager
    sm: ServiceManager = request.app["service_manager"]
    name = request.match_info["name"]
    try:
        sm.start_service(name)
        return web.Response(text=json.dumps({"ok": True}), content_type="application/json")
    except KeyError as exc:
        return web.Response(status=404, text=str(exc))


async def _service_logs_handler(request: web.Request) -> web.Response:
    from .process_manager import ServiceManager
    sm: ServiceManager = request.app["service_manager"]
    name = request.match_info["name"]
    try:
        log_path = sm.get_log_path(name)
    except KeyError as exc:
        return web.Response(status=404, text=str(exc))

    lines_param = request.query.get("lines", "100")
    try:
        num_lines = int(lines_param)
    except ValueError:
        num_lines = 100

    if not log_path.exists():
        return web.Response(text="", content_type="text/plain")

    all_lines = log_path.read_text().splitlines()
    tail = all_lines[-num_lines:] if len(all_lines) > num_lines else all_lines
    return web.Response(text="\n".join(tail) + "\n", content_type="text/plain")


# --- ProxyServer ---

class ProxyServer:
    def __init__(
        self,
        services_file: Path,
        http_port: int,
        bind: tuple[str, ...],
        service_manager: object | None = None,
    ) -> None:
        self._services_file = services_file
        self._http_port = http_port
        self._bind = bind
        self._service_manager = service_manager
        self._route_table = RouteTable()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._proxy_runners: list[web.AppRunner] = []
        self._admin_runner: web.AppRunner | None = None

    @property
    def route_table(self) -> RouteTable:
        return self._route_table

    def start(self) -> None:
        _configure_logging()
        self._route_table.reload(self._services_file, env=os.environ)

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        # Wait for servers to bind
        future = asyncio.run_coroutine_threadsafe(self._start_servers(), self._loop)
        future.result(timeout=10)

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _start_servers(self) -> None:
        proxy_app = make_proxy_app(self._route_table)
        admin_app = make_admin_app(self._route_table, self._services_file, self._service_manager)

        # Start proxy on each bind address
        for host in self._bind:
            runner = web.AppRunner(proxy_app)
            await runner.setup()
            site = web.TCPSite(runner, host, self._http_port)
            await site.start()
            self._proxy_runners.append(runner)
            logger.info("Proxy listening on %s:%d", host, self._http_port)

        # Start admin on localhost
        self._admin_runner = web.AppRunner(admin_app)
        await self._admin_runner.setup()
        admin_site = web.TCPSite(self._admin_runner, "127.0.0.1", ADMIN_PORT)
        await admin_site.start()
        logger.info("Admin listening on 127.0.0.1:%d", ADMIN_PORT)

    def stop(self) -> None:
        if self._loop is None or self._thread is None:
            return

        future = asyncio.run_coroutine_threadsafe(self._stop_servers(), self._loop)
        future.result(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    async def _stop_servers(self) -> None:
        for runner in self._proxy_runners:
            await runner.cleanup()
        if self._admin_runner is not None:
            await self._admin_runner.cleanup()
