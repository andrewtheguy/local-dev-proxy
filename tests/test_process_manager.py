from __future__ import annotations

from pathlib import Path

from local_dev_proxy.process_manager import ServiceManager
from local_dev_proxy.routes import RoutesManifest, ServiceDef

from conftest import idle_command


IDLE = idle_command()


def _manager(tmp_path: Path, **services: ServiceDef) -> ServiceManager:
    manifest = RoutesManifest(
        http_port=2800,
        bind=("127.0.0.1",),
        services=dict(services),
    )
    return ServiceManager(manifest, tmp_path / "logs", tmp_path)


def _status(manager: ServiceManager, name: str) -> str:
    return next(
        str(service["status"])
        for service in manager.get_status()
        if service["name"] == name
    )


def test_start_all_skips_services_with_auto_start_disabled(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        eager=ServiceDef(name="eager", command=IDLE),
        manual=ServiceDef(name="manual", command=IDLE, auto_start=False),
    )

    try:
        manager.start_all()

        assert _status(manager, "eager") == "running"
        assert _status(manager, "manual") == "stopped"
    finally:
        manager.stop_all()


def test_service_with_auto_start_disabled_stays_manually_controllable(
    tmp_path: Path,
) -> None:
    manager = _manager(
        tmp_path,
        manual=ServiceDef(name="manual", command=IDLE, auto_start=False),
    )

    try:
        manager.start_all()
        manager.start_service("manual")

        assert _status(manager, "manual") == "running"

        manager.stop_service("manual")
        assert _status(manager, "manual") == "stopped"
    finally:
        manager.stop_all()
