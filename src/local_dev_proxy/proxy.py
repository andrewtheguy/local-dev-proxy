from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import fnmatch
import html
import logging
import os
import re
import threading
from pathlib import Path
from collections.abc import Mapping

import aiohttp
from aiohttp import web

from .config import require_port, require_socket_path
from .routes import RouteConfigError, RoutesManifest, load_routes

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    pkg_logger = logging.getLogger("local_dev_proxy")
    if not pkg_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
            )
        )
        pkg_logger.addHandler(handler)
        pkg_logger.setLevel(logging.INFO)


@dataclass(frozen=True)
class ResolvedRoute:
    id: str
    host_patterns: tuple[str, ...]
    target_host: str | None = None
    target_port: int | None = None
    target_socket: str | None = None


def resolve_routes(
    manifest: RoutesManifest,
    env: Mapping[str, str] | None = None,
    socket_base_dir: Path | None = None,
) -> list[ResolvedRoute]:
    resolved: list[ResolvedRoute] = []
    seen_ports: dict[int, str] = {}
    seen_sockets: dict[str, str] = {}

    for service in manifest.services.values():
        if service.disabled:
            continue

        effective_env: dict[str, str] = dict(service.env)
        if env:
            effective_env.update(env)

        for route in service.routes:
            if route.target_socket is not None or route.target_socket_env is not None:
                if route.target_socket is not None:
                    socket_path = route.target_socket
                else:
                    assert route.target_socket_env is not None
                    socket_path = require_socket_path(
                        effective_env, route.target_socket_env
                    )
                if socket_base_dir is not None and not Path(socket_path).is_absolute():
                    socket_path = str((socket_base_dir / socket_path).resolve())

                if socket_path in seen_sockets:
                    raise RouteConfigError(
                        f"Socket {socket_path!r} used by both "
                        f"{seen_sockets[socket_path]} and {route.id}"
                    )
                seen_sockets[socket_path] = route.id

                resolved.append(
                    ResolvedRoute(
                        id=route.id,
                        host_patterns=route.hosts,
                        target_socket=socket_path,
                    )
                )
                continue

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


class _HostMatcher:
    """Immutable matcher precompiled from a route list.

    Exact hostnames resolve via a single dict lookup; wildcard patterns fall
    back to precompiled regexes in route order. Exact matches win over
    wildcards regardless of declaration order.
    """

    __slots__ = ("routes", "_exact", "_wildcards")

    def __init__(self, routes: list[ResolvedRoute]) -> None:
        self.routes = routes
        self._exact: dict[str, ResolvedRoute] = {}
        self._wildcards: list[tuple[re.Pattern[str], ResolvedRoute]] = []
        for route in routes:
            for pattern in route.host_patterns:
                pattern = pattern.lower()
                if any(ch in pattern for ch in "*?["):
                    self._wildcards.append(
                        (re.compile(fnmatch.translate(pattern)), route)
                    )
                else:
                    self._exact.setdefault(pattern, route)

    def match(self, host: str) -> ResolvedRoute | None:
        host = host.lower()
        route = self._exact.get(host)
        if route is not None:
            return route
        for pattern, wildcard_route in self._wildcards:
            if pattern.match(host):
                return wildcard_route
        return None


class RouteTable:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._routes: list[ResolvedRoute] = []
        self._matcher: _HostMatcher | None = None
        self._portal_html: str = ""

    @property
    def routes(self) -> list[ResolvedRoute]:
        with self._lock:
            return list(self._routes)

    @property
    def portal_html(self) -> str:
        return self._portal_html

    def reload(self, services_file: Path, env: Mapping[str, str] | None = None) -> None:
        manifest = load_routes(services_file)
        routes = resolve_routes(
            manifest, env, socket_base_dir=services_file.resolve().parent
        )
        portal = _build_portal_html(manifest, routes)
        matcher = _HostMatcher(routes)
        with self._lock:
            self._routes = routes
            self._matcher = matcher
            self._portal_html = portal

    def match(self, host: str) -> ResolvedRoute | None:
        # Lock-free fast path: the matcher is an immutable snapshot swapped
        # atomically on reload. Rebuild only if _routes was replaced without
        # going through reload().
        matcher = self._matcher
        if matcher is None or matcher.routes is not self._routes:
            with self._lock:
                matcher = self._matcher
                if matcher is None or matcher.routes is not self._routes:
                    matcher = _HostMatcher(self._routes)
                    self._matcher = matcher
        return matcher.match(host)


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
                    f'<li><a href="{html.escape(url)}">{html.escape(host)}</a></li>'
                )

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Local Dev Proxy</title></head><body>"
        "<h1>Local Dev Proxy</h1><ul>" + "\n".join(items) + "</ul></body></html>"
    )


# --- Proxy handler ---

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

WEBSOCKET_HANDSHAKE_HEADERS = frozenset(
    {
        "sec-websocket-extensions",
        "sec-websocket-key",
        "sec-websocket-protocol",
        "sec-websocket-version",
    }
)


def _filter_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


def _filter_websocket_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in HOP_BY_HOP
        and k.lower() not in WEBSOCKET_HANDSHAKE_HEADERS
        and k.lower() != "origin"
    }


def _get_websocket_protocols(request: web.Request) -> tuple[str, ...]:
    raw_protocols = request.headers.get("Sec-WebSocket-Protocol", "")
    return tuple(
        protocol.strip() for protocol in raw_protocols.split(",") if protocol.strip()
    )


def _get_websocket_close_args(msg: aiohttp.WSMessage) -> tuple[int, bytes]:
    code = msg.data if isinstance(msg.data, int) else aiohttp.WSCloseCode.OK
    extra = msg.extra
    message = extra.encode() if extra is not None else b""
    return code, message


def _get_websocket_error_detail(msg: aiohttp.WSMessage) -> object:
    return msg.data if msg.data is not None else msg.extra


class _UpstreamSessions:
    """Long-lived client sessions keyed by upstream target.

    One shared session (with keep-alive connection pooling) covers all TCP
    routes; each Unix socket gets its own session since the connector is
    bound to the socket path. Sessions are created lazily on first use and
    closed when the proxy app shuts down.
    """

    # Never time out the overall request: proxied responses may stream
    # (SSE, long polls) indefinitely. Only bound the connect itself.
    _TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30)

    def __init__(self) -> None:
        self._sessions: dict[str | None, aiohttp.ClientSession] = {}

    def get(self, route: ResolvedRoute) -> aiohttp.ClientSession:
        key = route.target_socket
        session = self._sessions.get(key)
        if session is None or session.closed:
            connector = (
                aiohttp.UnixConnector(path=key)
                if key is not None
                else aiohttp.TCPConnector(limit=512, limit_per_host=256)
            )
            session = aiohttp.ClientSession(
                connector=connector,
                timeout=self._TIMEOUT,
                auto_decompress=False,
            )
            self._sessions[key] = session
        return session

    async def close(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        await asyncio.gather(
            *(session.close() for session in sessions), return_exceptions=True
        )


def make_proxy_app(route_table: RouteTable) -> web.Application:
    app = web.Application()
    app["route_table"] = route_table
    sessions = _UpstreamSessions()
    app["upstream_sessions"] = sessions

    async def _close_sessions(_: web.Application) -> None:
        await sessions.close()

    app.on_cleanup.append(_close_sessions)
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

    if route.target_socket is not None:
        target_base = "http://localhost"
    else:
        assert route.target_host is not None
        assert route.target_port is not None
        host_part = (
            f"[{route.target_host}]" if ":" in route.target_host else route.target_host
        )
        target_base = f"http://{host_part}:{route.target_port}"
    path = request.match_info.get("path_info", "")
    target_url = f"{target_base}/{path}"
    if request.query_string:
        target_url += f"?{request.query_string}"

    # WebSocket upgrade
    if _is_websocket_upgrade(request):
        logger.info("%s %s %s -> ws %s", request.method, host, request.path, route.id)
        return await _proxy_websocket(request, target_url, route)

    resp = await _proxy_http(request, target_url, route)
    logger.info(
        "%s %s %s -> %s %d", request.method, host, request.path, route.id, resp.status
    )
    return resp


def _is_websocket_upgrade(request: web.Request) -> bool:
    upgrade = request.headers.get("Upgrade", "").lower()
    return upgrade == "websocket"


async def _proxy_http(
    request: web.Request, target_url: str, route: ResolvedRoute
) -> web.StreamResponse:
    sessions: _UpstreamSessions = request.app["upstream_sessions"]
    session = sessions.get(route)
    headers = _filter_headers(request.headers)
    body = request.content if request.can_read_body else None

    resp: web.StreamResponse | None = None
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
        if resp is None:
            return web.Response(status=502, text="Bad Gateway")
        # The response already started streaming; a 502 can no longer be
        # sent. Drop the connection so the client sees a truncated response
        # rather than a complete-looking one.
        resp.force_close()
        if request.transport is not None:
            request.transport.close()
        return resp


async def _proxy_websocket(
    request: web.Request, target_url: str, route: ResolvedRoute
) -> web.StreamResponse:
    ws_target = target_url.replace("http://", "ws://", 1).replace(
        "https://", "wss://", 1
    )
    sessions: _UpstreamSessions = request.app["upstream_sessions"]
    session = sessions.get(route)
    try:
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
            ws_response = web.WebSocketResponse(
                protocols=response_protocols, autoclose=False
            )
            await ws_response.prepare(request)

            async def _forward_client_to_upstream() -> tuple[str, int, bytes]:
                while True:
                    msg = await ws_response.receive()
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await ws_upstream.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await ws_upstream.send_bytes(msg.data)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                    ):
                        code, message = _get_websocket_close_args(msg)
                        return ("upstream", code, message)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.warning(
                            "WebSocket client error: %r",
                            _get_websocket_error_detail(msg),
                        )
                        return ("both", aiohttp.WSCloseCode.INTERNAL_ERROR, b"")
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        return ("upstream", aiohttp.WSCloseCode.OK, b"")

            async def _forward_upstream_to_client() -> tuple[str, int, bytes]:
                while True:
                    msg = await ws_upstream.receive()
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await ws_response.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await ws_response.send_bytes(msg.data)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                    ):
                        code, message = _get_websocket_close_args(msg)
                        return ("client", code, message)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.warning(
                            "WebSocket upstream error: %r",
                            _get_websocket_error_detail(msg),
                        )
                        return ("both", aiohttp.WSCloseCode.INTERNAL_ERROR, b"")
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        return ("client", aiohttp.WSCloseCode.OK, b"")

            client_task = asyncio.create_task(_forward_client_to_upstream())
            upstream_task = asyncio.create_task(_forward_upstream_to_client())
            try:
                done, pending = await asyncio.wait(
                    {client_task, upstream_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in pending:
                    task.cancel()
                for task in pending:
                    with suppress(asyncio.CancelledError, Exception):
                        await task

                # A forwarding task may have failed instead of returning a
                # close target; the WebSocket handshake already completed, so
                # the failure must be resolved here, not by the outer HTTP
                # error handlers.
                result: tuple[str, int, bytes] | None = None
                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        logger.warning("WebSocket forwarding failed: %r", exc)
                    elif result is None:
                        result = task.result()
                if result is None:
                    result = ("both", int(aiohttp.WSCloseCode.INTERNAL_ERROR), b"")
                close_target, code, message = result
            finally:
                client_task.cancel()
                upstream_task.cancel()

            if close_target == "upstream":
                await ws_response.close(code=code, message=message)
                await ws_upstream.close(code=code, message=message)
            else:
                await ws_upstream.close(code=code, message=message)
                await ws_response.close(code=code, message=message)
            return ws_response
    except aiohttp.WSServerHandshakeError as exc:
        logger.debug("WebSocket upstream rejected handshake: %s", exc)
        return web.Response(
            status=exc.status, text=exc.message or "WebSocket handshake failed"
        )
    except aiohttp.ClientError as exc:
        logger.debug("WebSocket upstream error: %s", exc)
        return web.Response(status=502, text="Bad Gateway")


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

        # Start proxy on each bind address
        for host in self._bind:
            runner = web.AppRunner(proxy_app)
            await runner.setup()
            site = web.TCPSite(runner, host, self._http_port)
            await site.start()
            self._proxy_runners.append(runner)
            logger.info("Proxy listening on %s:%d", host, self._http_port)

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
