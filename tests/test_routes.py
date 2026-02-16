from __future__ import annotations

from pathlib import Path

import pytest

from local_dev_proxy.routes import build_routes, load_routes


ROUTES_TOML = """
[caddy]
admin_url = "http://127.0.0.1:2019"
http_port = 2800
bind = ["127.0.0.1", "::1"]

[services.minio]
hosts = ["minios3.localhost", "*.minios3.localhost"]
target_port_env = "MINIO_PORT"

[services.s3browser]
hosts = ["s3browser.localhost"]
target_port_env = "S3BROWSER_PORT"

[services.minioconsole]
hosts = ["minioconsole.localhost"]
target_port_env = "MINIO_CONSOLE_PORT"
"""


def _write_manifest(tmp_path: Path) -> Path:
    manifest = tmp_path / "routes.toml"
    manifest.write_text(ROUTES_TOML)
    return manifest


def test_build_routes_is_deterministic_and_ports_are_resolved(tmp_path: Path) -> None:
    manifest = load_routes(_write_manifest(tmp_path))
    env = {
        "S3BROWSER_PORT": "18170",
        "MINIO_PORT": "19000",
        "MINIO_CONSOLE_PORT": "19001",
    }

    routes = build_routes(
        manifest,
        env,
        active_services={"minioconsole", "s3browser", "minio"},
    )

    assert [route.get("@id") for route in routes] == [
        "route-s3browser",
        "route-minio",
        "route-minioconsole",
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


def test_build_routes_raises_when_required_port_is_missing(tmp_path: Path) -> None:
    manifest = load_routes(_write_manifest(tmp_path))

    with pytest.raises(ValueError, match="S3BROWSER_PORT"):
        build_routes(manifest, env={}, active_services={"s3browser"})


def test_listen_addresses_include_ipv6_brackets(tmp_path: Path) -> None:
    manifest = load_routes(_write_manifest(tmp_path))
    assert manifest.caddy.listen_addresses() == ["127.0.0.1:2800", "[::1]:2800"]
