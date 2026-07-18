from __future__ import annotations

from pathlib import Path

import pytest

from local_dev_proxy.routes import RouteConfigError, load_routes, resolve_command


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_MANIFEST = PROJECT_ROOT / "src/local_dev_proxy/services.toml.sample"


SERVICES_TOML = """
http_port = 2800
bind = ["127.0.0.1", "::1"]

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


def test_manifest_fields(tmp_path: Path) -> None:
    manifest = load_routes(_write_manifest(tmp_path))

    assert manifest.http_port == 2800
    assert manifest.bind == ("127.0.0.1", "::1")
    assert "minio" in manifest.services
    assert "s3browser" in manifest.services


def test_bundled_sample_covers_supported_service_and_target_forms() -> None:
    manifest = load_routes(SAMPLE_MANIFEST)
    services = manifest.services

    assert services["minio"].command is not None
    assert services["vite"].command is None
    assert services["fixed_socket_example"].disabled
    assert services["worker_example"].disabled
    assert services["worker_example"].routes == []

    env_only_socket = services["env_only_socket_example"]
    assert env_only_socket.disabled
    assert env_only_socket.env == {"EXAMPLE_APP_SOCKET": "env-only-example.sock"}
    assert env_only_socket.command == ["placeholder-env-aware-http-server"]
    assert env_only_socket.routes[0].target_socket_env == "EXAMPLE_APP_SOCKET"

    inherited_env_socket = services["inherited_env_socket_example"]
    assert inherited_env_socket.disabled
    assert inherited_env_socket.env == {}
    assert inherited_env_socket.command == ["placeholder-env-aware-http-server"]
    assert (
        inherited_env_socket.routes[0].target_socket_env == "INHERITED_APP_SOCKET"
    )

    target_forms: set[str] = set()
    port_target_hosts: set[str | None] = set()
    has_exact_host = False
    has_wildcard_host = False
    for service in services.values():
        for route in service.routes:
            if route.target_port is not None:
                target_forms.add("target_port")
                port_target_hosts.add(route.target_host)
            elif route.target_port_env is not None:
                target_forms.add("target_port_env")
                port_target_hosts.add(route.target_host)
            elif route.target_socket is not None:
                target_forms.add("target_socket")
            elif route.target_socket_env is not None:
                target_forms.add("target_socket_env")

            has_exact_host |= any("*" not in host for host in route.hosts)
            has_wildcard_host |= any("*" in host for host in route.hosts)

    assert target_forms == {
        "target_port",
        "target_port_env",
        "target_socket",
        "target_socket_env",
    }
    assert port_target_hosts == {"localhost", "127.0.0.1", "::1"}
    assert has_exact_host
    assert has_wildcard_host


def test_manifest_requires_proxy_settings(tmp_path: Path) -> None:
    service = """
[services.app]
command = ["serve"]

[[services.app.routes]]
id = "app"
hosts = ["app.localhost"]
target_port = 3000
"""
    path = tmp_path / "services.toml"
    path.write_text(service)
    with pytest.raises(RouteConfigError, match="http_port is required"):
        load_routes(path)

    path.write_text(f"http_port = 2800\n{service}")
    with pytest.raises(RouteConfigError, match="bind is required"):
        load_routes(path)


def test_manifest_rejects_unknown_and_coerced_fields(tmp_path: Path) -> None:
    path = tmp_path / "services.toml"
    path.write_text(
        """
http_port = "2800"
bind = ["127.0.0.1"]

[services.app]
command = ["serve"]

[[services.app.routes]]
id = "app"
hosts = ["app.localhost"]
target_port = 3000
"""
    )
    with pytest.raises(RouteConfigError, match="http_port must be an integer"):
        load_routes(path)

    path.write_text(
        """
http_port = 2800
bind = ["127.0.0.1"]
old_admin_port = 2801

[services.app]
command = ["serve"]

[[services.app.routes]]
id = "app"
hosts = ["app.localhost"]
target_port = 3000
"""
    )
    with pytest.raises(RouteConfigError, match="unknown keys: old_admin_port"):
        load_routes(path)


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

    minio_svc = manifest.services["minio"]
    assert minio_svc.env["MINIO_PORT"] == "19000"
    assert minio_svc.env["MINIO_BROWSER_REDIRECT"] == "off"
    assert len(minio_svc.routes) == 2
    assert minio_svc.routes[0].id == "minio"
    assert minio_svc.routes[1].id == "minioconsole"


def test_target_port_fixed(tmp_path: Path) -> None:
    toml = """
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


def test_unmanaged_service_parses(tmp_path: Path) -> None:
    toml = """
http_port = 2800
bind = ["127.0.0.1"]

[services.external]
env = {APP_PORT = "3000"}

[[services.external.routes]]
id = "external"
hosts = ["external.localhost"]
target_port_env = "APP_PORT"
"""
    path = tmp_path / "services.toml"
    path.write_text(toml)
    manifest = load_routes(path)

    svc = manifest.services["external"]
    assert svc.command is None
    assert svc.env == {"APP_PORT": "3000"}
    assert len(svc.routes) == 1
    assert svc.routes[0].id == "external"


def test_command_empty_list_rejected(tmp_path: Path) -> None:
    toml = """
http_port = 2800
bind = ["127.0.0.1"]

[services.bad]
command = []

[[services.bad.routes]]
id = "bad"
hosts = ["bad.localhost"]
target_port = 3000
"""
    path = tmp_path / "services.toml"
    path.write_text(toml)

    with pytest.raises(
        RouteConfigError, match="services.bad.command must be a non-empty list"
    ):
        load_routes(path)


def test_target_port_rejects_both(tmp_path: Path) -> None:
    toml = """
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

    with pytest.raises(RouteConfigError, match="set exactly one"):
        load_routes(path)


def test_target_socket_fixed_and_env_parse(tmp_path: Path) -> None:
    toml = """
http_port = 2800
bind = ["127.0.0.1"]

[services.socket]
command = ["socket-server"]
env = {APP_SOCKET = "/tmp/app-env.sock"}

[[services.socket.routes]]
id = "fixed-socket"
hosts = ["fixed.localhost"]
target_socket = "/tmp/app.sock"

[[services.socket.routes]]
id = "env-socket"
hosts = ["env.localhost"]
target_socket_env = "APP_SOCKET"
"""
    path = tmp_path / "services.toml"
    path.write_text(toml)

    routes = load_routes(path).services["socket"].routes
    assert routes[0].target_socket == "/tmp/app.sock"
    assert routes[0].target_socket_env is None
    assert routes[0].target_host is None
    assert routes[1].target_socket is None
    assert routes[1].target_socket_env == "APP_SOCKET"
    assert routes[1].target_host is None


def test_target_requires_exactly_one_port_or_socket(tmp_path: Path) -> None:
    path = tmp_path / "services.toml"
    path.write_text(
        """
http_port = 2800
bind = ["127.0.0.1"]

[services.bad]
command = ["bad"]

[[services.bad.routes]]
id = "bad"
hosts = ["bad.localhost"]
target_port = 3000
target_socket = "/tmp/bad.sock"
"""
    )

    with pytest.raises(RouteConfigError, match="set exactly one"):
        load_routes(path)


def test_target_socket_rejects_target_host(tmp_path: Path) -> None:
    path = tmp_path / "services.toml"
    path.write_text(
        """
http_port = 2800
bind = ["127.0.0.1"]

[services.bad]
command = ["bad"]

[[services.bad.routes]]
id = "bad"
hosts = ["bad.localhost"]
target_host = "127.0.0.1"
target_socket = "/tmp/bad.sock"
"""
    )

    with pytest.raises(RouteConfigError, match="cannot be used with a Unix socket"):
        load_routes(path)
