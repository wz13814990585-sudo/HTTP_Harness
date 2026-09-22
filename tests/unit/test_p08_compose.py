"""The optional single-node topology must retain its development safety bounds."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_dev_compose_pins_images_and_only_publishes_loopback_api() -> None:
    manifest = yaml.safe_load((ROOT / "deploy/dev/compose.yaml").read_text())
    services = manifest["services"]
    assert services["db"]["image"].startswith("postgres:14.18@sha256:")
    assert "ports" not in services["db"]
    assert services["api"]["ports"] == ["127.0.0.1:${HNH_HTTP_PORT:-8080}:8080"]
    assert "ports" not in services["worker"]
    assert services["migrate"]["depends_on"]["db"]["condition"] == "service_healthy"
    assert services["api"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["worker"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert services["api"]["image"] == services["worker"]["image"]
    assert "HNH_DEEPSEEK_API_KEY" not in services["api"]["environment"]
    assert "HNH_DEEPSEEK_API_KEY" in services["worker"]["environment"]
    assert "TYPESAFE_API_KEY" not in services["api"]["environment"]
    assert "TYPESAFE_API_KEY" in services["worker"]["environment"]


def test_dev_compose_does_not_mount_host_or_docker_socket_into_agent_processes() -> None:
    manifest = yaml.safe_load((ROOT / "deploy/dev/compose.yaml").read_text())
    for name in ("api", "worker", "migrate"):
        service = manifest["services"][name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert not service.get("privileged", False)
        assert all(
            "/var/run/docker.sock" not in mount and not mount.startswith("/")
            for mount in service.get("volumes", [])
        )
    dockerfile = (ROOT / "deploy/dev/Dockerfile").read_text()
    assert dockerfile.splitlines()[0].startswith("FROM python:3.12.14-slim-bookworm@sha256:")
    assert "USER 10001:10001" in dockerfile
    ignore = (ROOT / ".dockerignore").read_text().splitlines()
    assert ignore[0] == "**"
    assert not any(line.startswith("!.env") or line.endswith("/.env") for line in ignore)
