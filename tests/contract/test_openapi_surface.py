from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fastapi.routing import APIRoute
from openapi_spec_validator import validate

from hnh.contracts import compare_surfaces, iter_operations
from hnh.transport.http.app import create_app


def _load_target(repository_root: Path) -> dict[str, Any]:
    document = yaml.safe_load(
        (repository_root / "contracts" / "openapi.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(document, dict)
    return document


def test_target_contract_is_valid_openapi_311(repository_root: Path) -> None:
    validate(_load_target(repository_root))


def test_generated_openapi_matches_actual_schema_routes() -> None:
    app = create_app()
    runtime = app.openapi()
    validate(runtime)

    generated = {(path, method) for path, method, _ in iter_operations(runtime)}
    actual = {
        (route.path.replace("{file_path:path}", "{file_path}"), method.lower())
        for route in app.routes
        if isinstance(route, APIRoute) and route.include_in_schema
        for method in route.methods
        if method.lower() != "head"
    }
    assert generated == actual


def test_only_implemented_target_routes_are_advertised(repository_root: Path) -> None:
    target = _load_target(repository_root)
    runtime = create_app().openapi()
    diff = compare_surfaces(target, runtime)

    assert not diff.operation_id_mismatches
    assert not diff.unexpected
    assert set(diff.implemented) == {
        "GET /v1/actions/{action_id}",
        "GET /v1/artifacts/{artifact_id}",
        "GET /v1/artifacts/{artifact_id}/content",
        "GET /v1/capabilities",
        "GET /v1/capabilities/{capability_id}",
        "GET /v1/execution-sessions/{session_id}",
        "GET /v1/input-requests/{input_request_id}",
        "GET /v1/openapi.json",
        "GET /v1/runs/{run_id}",
        "GET /v1/runs/{run_id}/event-history",
        "GET /v1/runs/{run_id}/events",
        "GET /v1/workspaces/{workspace_id}/files/{file_path}",
        "POST /v1/artifacts",
        "POST /v1/execution-sessions",
        "POST /v1/execution-sessions/{session_id}/executions",
        "POST /v1/input-requests/{input_request_id}/responses",
        "POST /v1/actions/{action_id}/reconciliations",
        "POST /v1/integrations/{integration_id}/operations/{operation_name}/invocations",
        "POST /v1/runs",
        "POST /v1/runs/{run_id}/cancellations",
        "PUT /v1/workspaces/{workspace_id}/files/{file_path}",
    }
    assert len(diff.unimplemented) == sum(1 for _ in iter_operations(target)) - 21


def test_sse_route_advertises_event_stream_media_type() -> None:
    operation = create_app().openapi()["paths"]["/v1/runs/{run_id}/events"]["get"]

    assert "text/event-stream" in operation["responses"]["200"]["content"]
    assert "application/json" not in operation["responses"]["200"]["content"]
