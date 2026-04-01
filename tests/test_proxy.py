from __future__ import annotations

from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from local_dev_proxy.proxy import (
    ResolvedRoute,
    RouteTable,
    make_admin_app,
    make_proxy_app,
    resolve_routes,
)
from local_dev_proxy.routes import RouteConfigError, load_routes


SERVICES_TOML = """
http_port = 2800
bind = ["127.0.0.1"]

[services.minio]
command = ["minio", "server", "data"]
env = {MINIO_PORT = "19000", MINIO_CONSOLE_PORT = "19001"}

[[services.minio.routes]]
id = "minio"
hosts = ["minios3.localhost", "*.minios3.localhost"]
target_port_env = "MINIO_PORT"

[[services.minio.routes]]
id = "minioconsole"
hosts = ["minioconsole.localhost"]
target_port_env = "MINIO_CONSOLE_PORT"

[services.s3browser]
command = ["s3browser"]
env = {S3BROWSER_PORT = "18170"}

[[services.s3browser.routes]]
id = "s3browser"
hosts = ["s3browser.localhost"]
target_port_env = "S3BROWSER_PORT"
"""


def _write_manifest(tmp_path: Path) -> Path:
    p = tmp_path / "services.toml"
    p.write_text(SERVICES_TOML)
    return p


# --- resolve_routes tests ---


def test_resolve_routes_resolves_ports(tmp_path: Path) -> None:
    manifest = load_routes(_write_manifest(tmp_path))
    routes = resolve_routes(manifest)

    by_id = {r.id: r for r in routes}
    assert by_id["minio"].target_port == 19000
    assert by_id["minioconsole"].target_port == 19001
    assert by_id["s3browser"].target_port == 18170


def test_resolve_routes_env_override(tmp_path: Path) -> None:
    manifest = load_routes(_write_manifest(tmp_path))
    routes = resolve_routes(manifest, env={"S3BROWSER_PORT": "9999"})

    by_id = {r.id: r for r in routes}
    assert by_id["s3browser"].target_port == 9999


def test_resolve_routes_duplicate_port(tmp_path: Path) -> None:
    manifest = load_routes(_write_manifest(tmp_path))

    with pytest.raises(RouteConfigError, match="Port 19000"):
        resolve_routes(manifest, env={"S3BROWSER_PORT": "19000"})


def test_resolve_routes_missing_port(tmp_path: Path) -> None:
    toml = """
[services.oops]
command = ["oops"]

[[services.oops.routes]]
id = "oops"
hosts = ["oops.localhost"]
target_port_env = "OOPS_PORT"
"""
    path = tmp_path / "services.toml"
    path.write_text(toml)
    manifest = load_routes(path)

    with pytest.raises(ValueError, match="OOPS_PORT"):
        resolve_routes(manifest)


# --- RouteTable tests ---


def test_route_table_match_exact() -> None:
    rt = RouteTable()
    rt._routes = [
        ResolvedRoute(id="minio", host_patterns=("minios3.localhost",), target_host="127.0.0.1", target_port=19000),
        ResolvedRoute(id="s3browser", host_patterns=("s3browser.localhost",), target_host="127.0.0.1", target_port=18170),
    ]

    match = rt.match("minios3.localhost")
    assert match is not None
    assert match.id == "minio"

    match = rt.match("s3browser.localhost")
    assert match is not None
    assert match.id == "s3browser"


def test_route_table_match_wildcard() -> None:
    rt = RouteTable()
    rt._routes = [
        ResolvedRoute(id="minio", host_patterns=("minios3.localhost", "*.minios3.localhost"), target_host="127.0.0.1", target_port=19000),
    ]

    assert rt.match("bucket.minios3.localhost") is not None
    assert rt.match("minios3.localhost") is not None


def test_route_table_match_unmatched() -> None:
    rt = RouteTable()
    rt._routes = [
        ResolvedRoute(id="minio", host_patterns=("minios3.localhost",), target_host="127.0.0.1", target_port=19000),
    ]

    assert rt.match("unknown.localhost") is None


def test_route_table_reload(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)
    rt = RouteTable()
    rt.reload(path)

    assert len(rt.routes) == 3
    assert rt.match("minios3.localhost") is not None
    assert "Local Dev Proxy" in rt.portal_html


# --- Proxy app tests (using pytest-aiohttp) ---


def _make_upstream_app() -> web.Application:
    """A simple upstream app that echoes back request info."""
    app = web.Application()

    async def echo_handler(request: web.Request) -> web.Response:
        body = await request.read()
        return web.Response(
            text=f"method={request.method} path={request.path} body={body.decode()}",
            headers={"X-Upstream": "true"},
        )

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await ws.send_str(f"echo:{msg.data}")
            elif msg.type == aiohttp.WSMsgType.CLOSE:
                break
        return ws

    app.router.add_route("*", "/ws", ws_handler)
    app.router.add_route("*", "/{path_info:.*}", echo_handler)
    return app


async def test_proxy_portal_on_localhost(aiohttp_client: type) -> None:
    upstream_app = _make_upstream_app()
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    rt = RouteTable()
    rt._routes = [
        ResolvedRoute(
            id="test-service",
            host_patterns=("test.localhost",),
            target_host="127.0.0.1",
            target_port=upstream_server.port,
        ),
    ]
    rt._portal_html = "<html><body>Portal</body></html>"

    proxy_app = make_proxy_app(rt)
    client = await aiohttp_client(proxy_app)
    try:
        resp = await client.get("/", headers={"Host": "localhost"})
        assert resp.status == 200
        text = await resp.text()
        assert "Portal" in text
    finally:
        await upstream_server.close()


async def test_proxy_forwards_http(aiohttp_client: type) -> None:
    upstream_app = _make_upstream_app()
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    rt = RouteTable()
    rt._routes = [
        ResolvedRoute(
            id="test-service",
            host_patterns=("test.localhost",),
            target_host="127.0.0.1",
            target_port=upstream_server.port,
        ),
    ]
    rt._portal_html = ""

    proxy_app = make_proxy_app(rt)
    client = await aiohttp_client(proxy_app)
    try:
        resp = await client.get("/some/path", headers={"Host": "test.localhost"})
        assert resp.status == 200
        text = await resp.text()
        assert "method=GET" in text
        assert "path=/some/path" in text
    finally:
        await upstream_server.close()


async def test_proxy_forwards_post_body(aiohttp_client: type) -> None:
    upstream_app = _make_upstream_app()
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    rt = RouteTable()
    rt._routes = [
        ResolvedRoute(
            id="test-service",
            host_patterns=("test.localhost",),
            target_host="127.0.0.1",
            target_port=upstream_server.port,
        ),
    ]
    rt._portal_html = ""

    proxy_app = make_proxy_app(rt)
    client = await aiohttp_client(proxy_app)
    try:
        resp = await client.post("/upload", headers={"Host": "test.localhost"}, data=b"hello")
        assert resp.status == 200
        text = await resp.text()
        assert "method=POST" in text
        assert "body=hello" in text
    finally:
        await upstream_server.close()


async def test_proxy_404_for_unknown_host(aiohttp_client: type) -> None:
    rt = RouteTable()
    rt._routes = [
        ResolvedRoute(
            id="test-service",
            host_patterns=("test.localhost",),
            target_host="127.0.0.1",
            target_port=9999,
        ),
    ]
    rt._portal_html = ""

    proxy_app = make_proxy_app(rt)
    client = await aiohttp_client(proxy_app)

    resp = await client.get("/", headers={"Host": "unknown.localhost"})
    assert resp.status == 404


async def test_proxy_websocket(aiohttp_client: type) -> None:
    upstream_app = _make_upstream_app()
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    rt = RouteTable()
    rt._routes = [
        ResolvedRoute(
            id="test-service",
            host_patterns=("test.localhost",),
            target_host="127.0.0.1",
            target_port=upstream_server.port,
        ),
    ]
    rt._portal_html = ""

    proxy_app = make_proxy_app(rt)
    client = await aiohttp_client(proxy_app)
    try:
        async with client.ws_connect("/ws", headers={"Host": "test.localhost"}) as ws:
            await ws.send_str("hello")
            msg = await ws.receive()
            assert msg.type == aiohttp.WSMsgType.TEXT
            assert msg.data == "echo:hello"
            await ws.close()
    finally:
        await upstream_server.close()


async def test_proxy_websocket_forwards_handshake_headers(aiohttp_client: type) -> None:
    async def ws_handler(request: web.Request) -> web.StreamResponse:
        if request.headers.get("Cookie") != "session=abc123":
            return web.Response(status=403, text="missing cookie")
        if request.headers.get("Origin") != "http://test.localhost":
            return web.Response(status=403, text="missing origin")
        if request.headers.get("Host") != "test.localhost":
            return web.Response(status=403, text="wrong host")

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str("ready")
        await ws.close()
        return ws

    upstream_app = web.Application()
    upstream_app.router.add_get("/ws", ws_handler)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    rt = RouteTable()
    rt._routes = [
        ResolvedRoute(
            id="test-service",
            host_patterns=("test.localhost",),
            target_host="127.0.0.1",
            target_port=upstream_server.port,
        ),
    ]
    rt._portal_html = ""

    proxy_app = make_proxy_app(rt)
    client = await aiohttp_client(proxy_app)
    try:
        async with client.ws_connect(
            "/ws",
            headers={
                "Host": "test.localhost",
                "Cookie": "session=abc123",
                "Origin": "http://test.localhost",
            },
        ) as ws:
            msg = await ws.receive()
            assert msg.type == aiohttp.WSMsgType.TEXT
            assert msg.data == "ready"
    finally:
        await upstream_server.close()


async def test_proxy_websocket_returns_upstream_handshake_failure(aiohttp_client: type) -> None:
    async def ws_handler(_: web.Request) -> web.StreamResponse:
        return web.Response(status=403, text="forbidden")

    upstream_app = web.Application()
    upstream_app.router.add_get("/ws", ws_handler)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    rt = RouteTable()
    rt._routes = [
        ResolvedRoute(
            id="test-service",
            host_patterns=("test.localhost",),
            target_host="127.0.0.1",
            target_port=upstream_server.port,
        ),
    ]
    rt._portal_html = ""

    proxy_app = make_proxy_app(rt)
    client = await aiohttp_client(proxy_app)
    try:
        resp = await client.get(
            "/ws",
            headers={
                "Host": "test.localhost",
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                "Sec-WebSocket-Version": "13",
            },
        )
        assert resp.status == 403
        assert await resp.text() == "Invalid response status"
    finally:
        await upstream_server.close()


async def test_proxy_502_when_upstream_down(aiohttp_client: type) -> None:
    rt = RouteTable()
    rt._routes = [
        ResolvedRoute(
            id="dead",
            host_patterns=("dead.localhost",),
            target_host="127.0.0.1",
            target_port=1,
        ),
    ]
    rt._portal_html = ""

    proxy_app = make_proxy_app(rt)
    client = await aiohttp_client(proxy_app)

    resp = await client.get("/", headers={"Host": "dead.localhost"})
    assert resp.status == 502


# --- Admin app tests ---


async def test_admin_healthz(aiohttp_client: type, tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)
    rt = RouteTable()
    admin_app = make_admin_app(rt, path)
    client = await aiohttp_client(admin_app)

    resp = await client.get("/healthz")
    assert resp.status == 200
    assert await resp.text() == "ok"


async def test_admin_reload(aiohttp_client: type, tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)
    rt = RouteTable()
    admin_app = make_admin_app(rt, path)
    client = await aiohttp_client(admin_app)

    resp = await client.post("/reload")
    assert resp.status == 200
    assert await resp.text() == "reloaded"

    # Verify routes were actually loaded
    assert len(rt.routes) == 3
