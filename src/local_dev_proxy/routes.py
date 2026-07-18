from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import tomllib
from typing import Mapping


class RouteConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ServiceRoute:
    id: str
    hosts: tuple[str, ...]
    target_host: str
    target_port: int | None = None
    target_port_env: str | None = None


@dataclass(frozen=True)
class ServiceDef:
    name: str
    command: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    routes: list[ServiceRoute] = field(default_factory=list)


@dataclass(frozen=True)
class RoutesManifest:
    http_port: int
    bind: tuple[str, ...]
    services: dict[str, ServiceDef]


def load_routes(path: Path) -> RoutesManifest:
    if not path.exists():
        raise RouteConfigError(f"Services manifest not found: {path}")

    return build_manifest(tomllib.loads(path.read_text()))


def validate_toml(text: str) -> RoutesManifest:
    """Parse and validate raw TOML text, raising RouteConfigError on any issue.

    Used by the config editor to check edits without touching the filesystem.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RouteConfigError(f"Invalid TOML: {exc}") from exc
    return build_manifest(data)


def build_manifest(data: dict[str, object]) -> RoutesManifest:
    http_port = _parse_int(data.get("http_port", 2800), "http_port")

    bind = data.get("bind", ["127.0.0.1", "::1"])
    if not isinstance(bind, list) or not bind:
        raise RouteConfigError("bind must be a non-empty list")
    bind_hosts = tuple(str(host) for host in bind)

    services_data = data.get("services")
    if not isinstance(services_data, dict) or not services_data:
        raise RouteConfigError("[services] must contain at least one service")

    services: dict[str, ServiceDef] = {}
    for name, raw in services_data.items():
        if not isinstance(raw, dict):
            raise RouteConfigError(f"services.{name} must be a table")

        command = raw.get("command")
        if command is not None:
            if not isinstance(command, list) or not command:
                raise RouteConfigError(f"services.{name}.command must be a non-empty list")

        env = raw.get("env", {})
        if not isinstance(env, dict):
            raise RouteConfigError(f"services.{name}.env must be a table")

        raw_routes = raw.get("routes", [])
        if not isinstance(raw_routes, list):
            raise RouteConfigError(f"services.{name}.routes must be an array")

        routes: list[ServiceRoute] = []
        for i, route_raw in enumerate(raw_routes):
            if not isinstance(route_raw, dict):
                raise RouteConfigError(f"services.{name}.routes[{i}] must be a table")

            route_id = route_raw.get("id")
            if not isinstance(route_id, str) or not route_id:
                raise RouteConfigError(
                    f"services.{name}.routes[{i}].id must be a non-empty string"
                )

            hosts = route_raw.get("hosts")
            if not isinstance(hosts, list) or not hosts:
                raise RouteConfigError(
                    f"services.{name}.routes[{i}].hosts must be a non-empty list"
                )

            raw_port = route_raw.get("target_port")
            raw_port_env = route_raw.get("target_port_env")
            prefix = f"services.{name}.routes[{i}]"

            if raw_port is not None and raw_port_env is not None:
                raise RouteConfigError(
                    f"{prefix}: set target_port or target_port_env, not both"
                )
            if raw_port is None and raw_port_env is None:
                raise RouteConfigError(
                    f"{prefix}: requires target_port or target_port_env"
                )

            target_port: int | None = None
            target_port_env: str | None = None
            if raw_port is not None:
                target_port = _parse_int(raw_port, f"{prefix}.target_port")
            else:
                if not isinstance(raw_port_env, str) or not raw_port_env:
                    raise RouteConfigError(
                        f"{prefix}.target_port_env must be a non-empty string"
                    )
                target_port_env = raw_port_env

            _ALLOWED_HOSTS = ("127.0.0.1", "::1", "localhost")
            target_host = str(route_raw.get("target_host", "localhost"))
            if target_host not in _ALLOWED_HOSTS:
                raise RouteConfigError(
                    f"{prefix}.target_host must be one of {_ALLOWED_HOSTS}, got: {target_host!r}"
                )

            routes.append(
                ServiceRoute(
                    id=route_id,
                    hosts=tuple(str(h) for h in hosts),
                    target_host=target_host,
                    target_port=target_port,
                    target_port_env=target_port_env,
                )
            )

        services[name] = ServiceDef(
            name=str(name),
            command=[str(c) for c in command] if command is not None else None,
            env={str(k): str(v) for k, v in env.items()},
            routes=routes,
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
    try:
        parsed = int(str(value))
    except ValueError as exc:
        raise RouteConfigError(f"{field_name} must be an integer") from exc

    if parsed < 1 or parsed > 65535:
        raise RouteConfigError(f"{field_name} must be in range 1-65535")
    return parsed
