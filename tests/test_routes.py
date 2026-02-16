from __future__ import annotations

from pathlib import Path

import pytest

from local_dev_proxy.routes import RouteConfigError, build_routes, load_routes, resolve_command


SERVICES_TOML = """
[caddy]
admin_url = "http://127.0.0.1:2019"
http_port = 2800
bind = ["127.0.0.1", "::1"]

[services.caddy]
command = ["caddy", "run", "--config", "config/caddy-bootstrap.json"]

[services.minio]
command = ["minio", "server", "data", "--address", ":{MINIO_PORT}"]
env = {MINIO_BROWSER_REDIRECT = "off", MINIO_PORT = "19000", MINIO_CONSOLE_PORT = "19001"}

[[services.minio.routes]]
id = "minio"
hosts = ["minios3.localhost", "*.minios3.localhost"]
target_port_env = "MINIO_PORT"

[[services.minio.routes]]
id = "minioconsole"
hosts = ["minioconsole.localhost"]
target_port_env = "MINIO_CONSOLE_PORT"

[services.s3browser]
command = ["s3browser", "-b", "127.0.0.1:{S3BROWSER_PORT}"]
env = {S3BROWSER_PORT = "18170"}

[[services.s3browser.routes]]
id = "s3browser"
hosts = ["s3browser.localhost"]
target_port_env = "S3BROWSER_PORT"
"""


def _write_manifest(tmp_path: Path) -> Path:
    manifest = tmp_path / "services.toml"
    manifest.write_text(SERVICES_TOML)
    return manifest


def test_build_routes_is_deterministic_and_ports_are_resolved(tmp_path: Path) -> None:
    manifest = load_routes(_write_manifest(tmp_path))

    routes = build_routes(manifest)

    assert [route.get("@id") for route in routes] == [
        "route-minio",
        "route-minioconsole",
        "route-s3browser",
        "route-fallback",
    ]

    upstreams = {
        route["@id"]: route["handle"][0]["upstreams"][0]["dial"]
        for route in routes
        if route.get("@id") != "route-fallback"
    }
    assert upstreams == {
        "route-s3browser": "127.0.0.1:18170",
        "route-minio": "127.0.0.1:19000",
        "route-minioconsole": "127.0.0.1:19001",
    }


def test_build_routes_env_override(tmp_path: Path) -> None:
    manifest = load_routes(_write_manifest(tmp_path))

    routes = build_routes(manifest, env_override={"S3BROWSER_PORT": "9999"})

    upstreams = {
        route["@id"]: route["handle"][0]["upstreams"][0]["dial"]
        for route in routes
        if route.get("@id") != "route-fallback"
    }
    assert upstreams["route-s3browser"] == "127.0.0.1:9999"


def test_build_routes_raises_when_required_port_is_missing(tmp_path: Path) -> None:
    toml = """
[caddy]
admin_url = "http://127.0.0.1:2019"
http_port = 2800
bind = ["127.0.0.1"]

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
        build_routes(manifest)


def test_listen_addresses_include_ipv6_brackets(tmp_path: Path) -> None:
    manifest = load_routes(_write_manifest(tmp_path))
    assert manifest.caddy.listen_addresses() == ["127.0.0.1:2800", "[::1]:2800"]


def test_resolve_command_replaces_placeholders() -> None:
    command = ["minio", "server", "--address", ":{MINIO_PORT}"]
    env = {"MINIO_PORT": "19000"}
    assert resolve_command(command, env) == ["minio", "server", "--address", ":19000"]


def test_resolve_command_raises_on_missing_var() -> None:
    command = ["s3browser", "-b", "127.0.0.1:{S3BROWSER_PORT}"]
    with pytest.raises(ValueError, match="S3BROWSER_PORT"):
        resolve_command(command, env={})


def test_service_def_fields(tmp_path: Path) -> None:
    manifest = load_routes(_write_manifest(tmp_path))

    caddy_svc = manifest.services["caddy"]
    assert caddy_svc.command == ["caddy", "run", "--config", "config/caddy-bootstrap.json"]
    assert caddy_svc.routes == []
    assert caddy_svc.env == {}

    minio_svc = manifest.services["minio"]
    assert minio_svc.env["MINIO_PORT"] == "19000"
    assert minio_svc.env["MINIO_BROWSER_REDIRECT"] == "off"
    assert len(minio_svc.routes) == 2
    assert minio_svc.routes[0].id == "minio"
    assert minio_svc.routes[1].id == "minioconsole"


def test_build_routes_raises_on_duplicate_ports(tmp_path: Path) -> None:
    manifest = load_routes(_write_manifest(tmp_path))

    with pytest.raises(RouteConfigError, match="Port 19000 used by both minio and s3browser"):
        build_routes(manifest, env_override={"S3BROWSER_PORT": "19000"})


def test_target_port_fixed(tmp_path: Path) -> None:
    toml = """
[caddy]
admin_url = "http://127.0.0.1:2019"
http_port = 2800
bind = ["127.0.0.1"]

[services.fixed]
command = ["fixed-server"]

[[services.fixed.routes]]
id = "fixed"
hosts = ["fixed.localhost"]
target_port = 3000
"""
    path = tmp_path / "services.toml"
    path.write_text(toml)
    manifest = load_routes(path)

    assert manifest.services["fixed"].routes[0].target_port == 3000
    assert manifest.services["fixed"].routes[0].target_port_env is None

    routes = build_routes(manifest)
    assert routes[0]["handle"][0]["upstreams"][0]["dial"] == "127.0.0.1:3000"


def test_target_port_rejects_both(tmp_path: Path) -> None:
    toml = """
[caddy]
http_port = 2800
bind = ["127.0.0.1"]

[services.bad]
command = ["bad"]

[[services.bad.routes]]
id = "bad"
hosts = ["bad.localhost"]
target_port = 3000
target_port_env = "BAD_PORT"
"""
    path = tmp_path / "services.toml"
    path.write_text(toml)

    with pytest.raises(RouteConfigError, match="not both"):
        load_routes(path)
