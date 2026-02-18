from __future__ import annotations

import asyncio
from dataclasses import dataclass
from fnmatch import fnmatch
import html
import logging
import os
import threading
from pathlib import Path
from typing import Mapping

import aiohttp
from aiohttp import web

from .config import require_port
from .routes import RouteConfigError, RoutesManifest, load_routes

logger = logging.getLogger(__name__)

ADMIN_PORT = 24019


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


def _filter_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


def make_proxy_app(route_table: RouteTable) -> web.Application:
    app = web.Application()
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
        return web.Response(
            text=route_table.portal_html,
            content_type="text/html",
            charset="utf-8",
        )

    route = route_table.match(host)
    if route is None:
        return web.Response(status=404, text="Not Found")

    target_base = f"http://{route.target_host}:{route.target_port}"
    path = request.match_info.get("path_info", "")
    target_url = f"{target_base}/{path}"
    if request.query_string:
        target_url += f"?{request.query_string}"

    # WebSocket upgrade
    if _is_websocket_upgrade(request):
        return await _proxy_websocket(request, target_url)

    return await _proxy_http(request, target_url)


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


async def _proxy_websocket(request: web.Request, target_url: str) -> web.WebSocketResponse:
    ws_target = target_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    ws_response = web.WebSocketResponse()
    await ws_response.prepare(request)

    session = aiohttp.ClientSession()
    try:
        async with session.ws_connect(ws_target) as ws_upstream:

            async def _forward_client_to_upstream() -> None:
                async for msg in ws_response:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await ws_upstream.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await ws_upstream.send_bytes(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                        await ws_upstream.close()
                        return

            async def _forward_upstream_to_client() -> None:
                async for msg in ws_upstream:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await ws_response.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await ws_response.send_bytes(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                        await ws_response.close()
                        return

            await asyncio.gather(
                _forward_client_to_upstream(),
                _forward_upstream_to_client(),
                return_exceptions=True,
            )
    except aiohttp.ClientError as exc:
        logger.debug("WebSocket upstream error: %s", exc)
        if not ws_response.closed:
            await ws_response.close()
    finally:
        await session.close()

    return ws_response


# --- Admin handler ---

def make_admin_app(route_table: RouteTable, services_file: Path) -> web.Application:
    app = web.Application()
    app["route_table"] = route_table
    app["services_file"] = services_file
    app.router.add_get("/healthz", _healthz_handler)
    app.router.add_post("/reload", _reload_handler)
    return app


async def _healthz_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _reload_handler(request: web.Request) -> web.Response:
    route_table: RouteTable = request.app["route_table"]
    services_file: Path = request.app["services_file"]
    try:
        route_table.reload(services_file, env=os.environ)
        return web.Response(text="reloaded")
    except Exception as exc:
        logger.exception("Reload failed")
        return web.Response(status=500, text=str(exc))


# --- ProxyServer ---

class ProxyServer:
    def __init__(
        self,
        services_file: Path,
        http_port: int,
        bind: tuple[str, ...],
    ) -> None:
        self._services_file = services_file
        self._http_port = http_port
        self._bind = bind
        self._route_table = RouteTable()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._proxy_runners: list[web.AppRunner] = []
        self._admin_runner: web.AppRunner | None = None

    @property
    def route_table(self) -> RouteTable:
        return self._route_table

    def start(self) -> None:
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
        admin_app = make_admin_app(self._route_table, self._services_file)

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
