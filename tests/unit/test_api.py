from __future__ import annotations

from fastapi.testclient import TestClient

from hnh.application.auth import DevelopmentAuthenticator, DevelopmentPrincipal
from hnh.config import Settings
from hnh.transport.http.app import create_app

NO_EXTERNAL_CONFIGURATION = Settings(database_url=None, development_token=None)


def test_health_reports_database_configuration() -> None:
    response = TestClient(create_app(settings=NO_EXTERNAL_CONFIGURATION)).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "stage": "P05",
        "database_configured": False,
        "sandbox_configured": False,
    }


def test_run_route_fails_closed_without_database() -> None:
    auth = DevelopmentAuthenticator(
        {"test-token": DevelopmentPrincipal("tenant", "subject", frozenset({"runs:write"}))}
    )
    response = TestClient(create_app(settings=NO_EXTERNAL_CONFIGURATION, authenticator=auth)).post(
        "/v1/runs",
        headers={
            "Authorization": "Bearer test-token",
            "Idempotency-Key": "p01-no-database",
        },
        json={"agent_id": "demo", "input": "do work"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "dependency_unavailable"
