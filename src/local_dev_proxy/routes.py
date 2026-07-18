from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import tomllib
from collections.abc import Mapping

_TOP_LEVEL_KEYS = frozenset({"http_port", "bind", "services"})
_SERVICE_KEYS = frozenset({"command", "env", "routes", "disabled"})
_ROUTE_KEYS = frozenset(
    {
        "id",
        "hosts",
        "target_host",
        "target_port",
        "target_port_env",
        "target_socket",
        "target_socket_env",
    }
)
_ALLOWED_TARGET_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class RouteConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ServiceRoute:
    id: str
    hosts: tuple[str, ...]
    target_host: str | None = None
    target_port: int | None = None
    target_port_env: str | None = None
    target_socket: str | None = None
    target_socket_env: str | None = None


@dataclass(frozen=True)
class ServiceDef:
    name: str
    command: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    routes: list[ServiceRoute] = field(default_factory=list)
    disabled: bool = False


@dataclass(frozen=True)
class RoutesManifest:
    http_port: int
    bind: tuple[str, ...]
    services: dict[str, ServiceDef]


def load_routes(path: Path) -> RoutesManifest:
    if not path.exists():
        raise RouteConfigError(f"Services manifest not found: {path}")

    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise RouteConfigError(f"Invalid TOML: {exc}") from exc
    return _build_manifest(data)


def validate_toml(text: str) -> RoutesManifest:
    """Parse and validate raw TOML text, raising RouteConfigError on any issue.

    Used by the config editor to check edits without touching the filesystem.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RouteConfigError(f"Invalid TOML: {exc}") from exc
    return _build_manifest(data)


def _build_manifest(data: dict[str, object]) -> RoutesManifest:
    _reject_unknown_keys(data, _TOP_LEVEL_KEYS, "top level")
    if "http_port" not in data:
        raise RouteConfigError("http_port is required")
    if "bind" not in data:
        raise RouteConfigError("bind is required")

    http_port = _parse_int(data["http_port"], "http_port")

    bind_hosts = tuple(_parse_string_list(data["bind"], "bind"))

    services_data = data.get("services")
    if not isinstance(services_data, dict) or not services_data:
        raise RouteConfigError("[services] must contain at least one service")

    services: dict[str, ServiceDef] = {}
    for name, raw in services_data.items():
        if not isinstance(raw, dict):
            raise RouteConfigError(f"services.{name} must be a table")
        prefix = f"services.{name}"
        _reject_unknown_keys(raw, _SERVICE_KEYS, prefix)

        command = raw.get("command")
        parsed_command = (
            _parse_string_list(command, f"{prefix}.command")
            if command is not None
            else None
        )

        env = _parse_string_map(raw.get("env", {}), f"{prefix}.env")

        disabled = raw.get("disabled", False)
        if not isinstance(disabled, bool):
            raise RouteConfigError(f"{prefix}.disabled must be a boolean")

        raw_routes = raw.get("routes", [])
        if not isinstance(raw_routes, list):
            raise RouteConfigError(f"{prefix}.routes must be an array")

        routes: list[ServiceRoute] = []
        for i, route_raw in enumerate(raw_routes):
            if not isinstance(route_raw, dict):
                raise RouteConfigError(f"{prefix}.routes[{i}] must be a table")
            route_prefix = f"{prefix}.routes[{i}]"
            _reject_unknown_keys(route_raw, _ROUTE_KEYS, route_prefix)

            route_id = route_raw.get("id")
            if not isinstance(route_id, str) or not route_id:
                raise RouteConfigError(f"{route_prefix}.id must be a non-empty string")

            hosts = _parse_string_list(route_raw.get("hosts"), f"{route_prefix}.hosts")

            raw_port = route_raw.get("target_port")
            raw_port_env = route_raw.get("target_port_env")
            raw_socket = route_raw.get("target_socket")
            raw_socket_env = route_raw.get("target_socket_env")

            target_fields = {
                "target_port": raw_port,
                "target_port_env": raw_port_env,
                "target_socket": raw_socket,
                "target_socket_env": raw_socket_env,
            }
            selected_targets = [
                name for name, value in target_fields.items() if value is not None
            ]
            if len(selected_targets) != 1:
                raise RouteConfigError(
                    f"{route_prefix}: set exactly one of target_port, "
                    "target_port_env, target_socket, or target_socket_env"
                )

            target_port: int | None = None
            target_port_env: str | None = None
            target_socket: str | None = None
            target_socket_env: str | None = None
            target_host: str | None = None
            if selected_targets[0] == "target_port":
                target_port = _parse_int(raw_port, f"{route_prefix}.target_port")
            elif selected_targets[0] == "target_port_env":
                if not isinstance(raw_port_env, str) or not raw_port_env:
                    raise RouteConfigError(
                        f"{route_prefix}.target_port_env must be a non-empty string"
                    )
                target_port_env = raw_port_env
            elif selected_targets[0] == "target_socket":
                if not isinstance(raw_socket, str) or not raw_socket:
                    raise RouteConfigError(
                        f"{route_prefix}.target_socket must be a non-empty string"
                    )
                target_socket = raw_socket
            else:
                if not isinstance(raw_socket_env, str) or not raw_socket_env:
                    raise RouteConfigError(
                        f"{route_prefix}.target_socket_env must be a non-empty string"
                    )
                target_socket_env = raw_socket_env

            if target_port is not None or target_port_env is not None:
                target_host = route_raw.get("target_host", "localhost")
                if (
                    not isinstance(target_host, str)
                    or target_host not in _ALLOWED_TARGET_HOSTS
                ):
                    raise RouteConfigError(
                        f"{route_prefix}.target_host must be one of "
                        f"{sorted(_ALLOWED_TARGET_HOSTS)}, got: {target_host!r}"
                    )
            elif "target_host" in route_raw:
                raise RouteConfigError(
                    f"{route_prefix}.target_host cannot be used with a Unix socket"
                )

            routes.append(
                ServiceRoute(
                    id=route_id,
                    hosts=tuple(hosts),
                    target_host=target_host,
                    target_port=target_port,
                    target_port_env=target_port_env,
                    target_socket=target_socket,
                    target_socket_env=target_socket_env,
                )
            )

        services[name] = ServiceDef(
            name=name,
            command=parsed_command,
            env=env,
            routes=routes,
            disabled=disabled,
        )

    return RoutesManifest(
        http_port=http_port,
        bind=bind_hosts,
        services=services,
    )


def resolve_command(command: list[str], env: Mapping[str, str]) -> list[str]:
    """Replace {VAR} placeholders in command args with values from env."""

    def _substitute(arg: str) -> str:
        def replacer(match: re.Match[str]) -> str:
            key = match.group(1)
            value = env.get(key)
            if value is None:
                raise ValueError(f"Missing env variable for command placeholder: {key}")
            return value

        return re.sub(r"\{([A-Z_][A-Z0-9_]*)\}", replacer, arg)

    return [_substitute(arg) for arg in command]


def _parse_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RouteConfigError(f"{field_name} must be an integer")
    if value < 1 or value > 65535:
        raise RouteConfigError(f"{field_name} must be in range 1-65535")
    return value


def _parse_string_list(value: object, field_name: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise RouteConfigError(f"{field_name} must be a non-empty list of strings")
    return value


def _parse_string_map(value: object, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise RouteConfigError(f"{field_name} must be a table of string values")
    return value


def _reject_unknown_keys(
    table: Mapping[str, object], allowed: frozenset[str], field_name: str
) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise RouteConfigError(
            f"{field_name} contains unknown keys: {', '.join(sorted(unknown))}"
        )
