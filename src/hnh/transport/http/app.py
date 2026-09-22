"""ASGI application factory for the implemented P00-P05 resource surface."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, Query, Request, Response, Security
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from opentelemetry.propagate import extract
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind
from sqlalchemy import Engine
from sqlalchemy import text as sql_text

from hnh.adapters.blobs.filesystem import FileBlobStore
from hnh.adapters.execution.docker import DockerExecutionBroker
from hnh.adapters.execution.unavailable import UnavailableExecutionBroker
from hnh.adapters.postgres.database import build_engine
from hnh.application.action_gateway import ActionGateway
from hnh.application.auth import DevelopmentAuthenticator
from hnh.application.capabilities import CapabilityRegistry
from hnh.application.execution import ExecutionService
from hnh.application.mcp_composition import MCPIntegrationManager
from hnh.application.operations import HttpOperation
from hnh.application.resource_dispatcher import ResourceDispatcher
from hnh.application.resources import ResourceStore
from hnh.application.run_controller import RunController
from hnh.config import Settings
from hnh.domain.errors import DependencyUnavailable, HarnessError, UnsupportedMediaType
from hnh.domain.identity import TrustedContext
from hnh.ports.execution import BlobStore, ExecutionBroker, SandboxProfile
from hnh.transport.http.observability import (
    HTTPMetrics,
    build_otlp_provider,
    response_traceparent,
    span_traceparent,
)
from hnh.transport.http.schemas import (
    AcceptedModel,
    CancelCreateModel,
    EventModel,
    EventPageModel,
    ExecutionSessionModel,
    InputRequestModel,
    InputResponseModel,
    ReconciliationModel,
    RunCreateModel,
    RunModel,
    SessionCreateModel,
)

PROBLEM_RESPONSE = {
    "description": "Request rejected; inspect the application/problem+json code.",
    "content": {"application/problem+json": {"schema": {"type": "object"}}},
}
bearer = HTTPBearer(auto_error=False, scheme_name="BearerAuth")


def _parse_version_etag(value: str) -> int:
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] == '"':
        normalized = normalized[1:-1]
    if not normalized.isdigit():
        from hnh.domain.errors import SchemaInvalid

        raise SchemaInvalid("If-Match must contain the current numeric resource version")
    return int(normalized)


def create_app(
    *,
    settings: Settings | None = None,
    engine: Engine | None = None,
    authenticator: DevelopmentAuthenticator | None = None,
    execution_broker: ExecutionBroker | None = None,
    blob_store: BlobStore | None = None,
    mcp_integrations: MCPIntegrationManager | None = None,
    tracer_provider: TracerProvider | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_environment()
    resolved_authenticator = authenticator or resolved_settings.authenticator()
    resolved_engine = engine
    if resolved_engine is None and resolved_settings.database_url:
        resolved_engine = build_engine(resolved_settings.database_url)
    resolved_blob_store = blob_store
    if resolved_blob_store is None and resolved_settings.blob_root:
        resolved_blob_store = FileBlobStore(Path(resolved_settings.blob_root))
    resolved_broker = execution_broker
    if resolved_broker is None:
        if resolved_settings.sandbox_image:
            resolved_broker = DockerExecutionBroker(
                (
                    SandboxProfile(
                        resolved_settings.sandbox_profile_id,
                        resolved_settings.sandbox_image,
                    ),
                )
            )
        else:
            resolved_broker = UnavailableExecutionBroker()
    controller = RunController(resolved_engine) if resolved_engine is not None else None
    registry = CapabilityRegistry()
    resources = (
        ResourceStore(resolved_engine, resolved_blob_store) if resolved_engine is not None else None
    )
    execution_service = (
        ExecutionService(controller, resources, resolved_broker)
        if controller is not None and resources is not None
        else None
    )
    gateway = (
        ActionGateway(
            resolved_engine,
            registry,
            resources,
            controller,
            execution_service,
        )
        if resolved_engine is not None and resources is not None and controller is not None
        else None
    )
    dispatcher = (
        ResourceDispatcher(gateway, resources)
        if gateway is not None and resources is not None
        else None
    )

    app = FastAPI(
        title="HTTP-native Harness",
        version="0.1.0.dev0",
        description=(
            "Implemented runtime surface; see contracts/openapi.yaml for the target contract."
        ),
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.state.run_controller = controller
    app.state.authenticator = resolved_authenticator
    app.state.capability_registry = registry
    app.state.resource_store = resources
    app.state.blob_store = resolved_blob_store
    app.state.execution_broker = resolved_broker
    app.state.execution_service = execution_service
    app.state.action_gateway = gateway
    app.state.resource_dispatcher = dispatcher
    metrics = HTTPMetrics()
    app.state.http_metrics = metrics
    configured_provider = tracer_provider
    if configured_provider is None and resolved_settings.otlp_traces_endpoint:
        configured_provider = build_otlp_provider(resolved_settings.otlp_traces_endpoint)
        app.router.add_event_handler("shutdown", configured_provider.shutdown)
    app.state.tracer_provider = configured_provider
    if execution_service is not None:
        app.router.add_event_handler("shutdown", execution_service.close)

    @app.middleware("http")
    async def request_observability(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        status = 500
        span = None
        span_scope = None
        if configured_provider is not None:
            parent = extract({"traceparent": request.headers.get("traceparent", "")})
            tracer = configured_provider.get_tracer("hnh.transport.http")
            span_scope = tracer.start_as_current_span(
                "HTTP request", context=parent, kind=SpanKind.SERVER
            )
            span = span_scope.__enter__()
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["traceparent"] = (
                span_traceparent(span.get_span_context())
                if span is not None
                else response_traceparent(request.headers.get("traceparent"))
            )
            return response
        finally:
            route = request.scope.get("route")
            template = route.path if route is not None else "_unmatched"
            metrics.observe(request.method, template, status, time.perf_counter() - started)
            if span is not None and span_scope is not None:
                span.update_name(f"{request.method} {template}")
                span.set_attribute("http.request.method", request.method)
                span.set_attribute("http.route", template)
                span.set_attribute("http.response.status_code", status)
                span_scope.__exit__(None, None, None)

    @app.exception_handler(HarnessError)
    async def handle_harness_error(_request: Request, exc: HarnessError) -> JSONResponse:
        body: dict[str, Any] = {
            "type": f"urn:hnh:problem:{exc.code}",
            "title": exc.title,
            "status": exc.status,
            "code": exc.code,
            "retry_hint": exc.retry_hint,
        }
        if exc.detail is not None:
            body["detail"] = exc.detail
        body.update(exc.extensions)
        headers: dict[str, str] = {}
        if exc.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        history_url = exc.extensions.get("history_url")
        snapshot_url = exc.extensions.get("snapshot_url")
        if isinstance(history_url, str) and isinstance(snapshot_url, str):
            headers["Link"] = f'<{history_url}>; rel="history", <{snapshot_url}>; rel="snapshot"'
        return JSONResponse(
            status_code=exc.status,
            content=body,
            media_type="application/problem+json",
            headers=headers or None,
        )

    def require_scope(scope: str) -> Callable[..., TrustedContext]:
        def dependency(
            credentials: HTTPAuthorizationCredentials | None = Security(bearer),
        ) -> TrustedContext:
            authorization = (
                None if credentials is None else f"{credentials.scheme} {credentials.credentials}"
            )
            return resolved_authenticator.authenticate(authorization, scope)

        return dependency

    def require_controller() -> RunController:
        if controller is None:
            raise DependencyUnavailable("PostgreSQL")
        return controller

    def require_resources() -> ResourceStore:
        if resources is None:
            raise DependencyUnavailable("PostgreSQL")
        return resources

    def require_gateway() -> ActionGateway:
        if gateway is None:
            raise DependencyUnavailable("PostgreSQL")
        return gateway

    def require_dispatcher() -> ResourceDispatcher:
        if dispatcher is None:
            raise DependencyUnavailable("PostgreSQL")
        return dispatcher

    def require_execution_service() -> ExecutionService:
        if execution_service is None:
            raise DependencyUnavailable("PostgreSQL")
        return execution_service

    def scoped_registry(context: TrustedContext) -> CapabilityRegistry:
        if mcp_integrations is None:
            return registry
        service = require_controller()
        return mcp_integrations.for_context(context, service)[0]

    def scoped_integration_dispatcher(context: TrustedContext) -> ResourceDispatcher:
        if mcp_integrations is None:
            return require_dispatcher()
        service = require_controller()
        resource_store = require_resources()
        engine_value = resolved_engine
        if engine_value is None:
            raise DependencyUnavailable("PostgreSQL")
        scoped_catalog, integration_executor = mcp_integrations.for_context(context, service)
        scoped_gateway = ActionGateway(
            engine_value,
            scoped_catalog,
            resource_store,
            service,
            execution_service,
            integration_executor,
        )
        return ResourceDispatcher(scoped_gateway, resource_store)

    @app.get("/healthz", operation_id="getHealth", include_in_schema=False)
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "stage": "P05",
            "database_configured": controller is not None,
            "sandbox_configured": resolved_settings.sandbox_image is not None
            or execution_broker is not None,
        }

    @app.get("/readyz", operation_id="getReadiness", include_in_schema=False)
    def readiness() -> JSONResponse:
        database_healthy = False
        if resolved_engine is not None:
            try:
                with resolved_engine.connect() as connection:
                    connection.execute(sql_text("SELECT 1"))
                database_healthy = True
            except Exception:
                database_healthy = False
        configured_model = bool(
            resolved_settings.deepseek_api_key and resolved_settings.deepseek_model
        )
        configured_broker = not isinstance(resolved_broker, UnavailableExecutionBroker)
        body = {
            "core_ready": database_healthy,
            "components": {
                "database": "ready" if database_healthy else "unavailable",
                "model_configuration": "configured" if configured_model else "missing",
                "isolated_broker_configuration": "configured" if configured_broker else "missing",
                "blob_store_configuration": "configured" if resolved_blob_store else "missing",
                "mcp_integrations": "configured" if mcp_integrations else "disabled",
            },
            "note": "Configuration is not proof of remote model, broker, or MCP health.",
        }
        return JSONResponse(status_code=200 if database_healthy else 503, content=body)

    @app.get("/metrics", operation_id="getMetrics", include_in_schema=False)
    def get_metrics(
        _context: TrustedContext = Depends(require_scope("metrics:read")),
    ) -> Response:
        return Response(metrics.render(), media_type="text/plain; version=0.0.4")

    @app.get(
        "/v1/capabilities",
        operation_id="listCapabilities",
        responses={"default": PROBLEM_RESPONSE},
    )
    def list_capabilities(
        cursor: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=100),
        context: TrustedContext = Depends(require_scope("capabilities:read")),
    ) -> dict[str, Any]:
        page = scoped_registry(context).list(context, cursor, limit)
        return {
            "items": [item.public() for item in page.items],
            "catalog_revision": page.catalog_revision,
            "next_cursor": page.next_cursor,
        }

    @app.get(
        "/v1/capabilities/{capability_id}",
        operation_id="getCapability",
        responses={"default": PROBLEM_RESPONSE},
    )
    def get_capability(
        capability_id: str,
        context: TrustedContext = Depends(require_scope("capabilities:read")),
    ) -> dict[str, Any]:
        return scoped_registry(context).get(context, capability_id).public()

    @app.get(
        "/v1/openapi.json",
        operation_id="getOpenAPI",
        responses={"default": PROBLEM_RESPONSE},
    )
    def get_filtered_openapi(
        context: TrustedContext = Depends(require_scope("capabilities:read")),
    ) -> dict[str, Any]:
        return scoped_registry(context).openapi(context)

    @app.post(
        "/v1/runs",
        operation_id="createRun",
        status_code=202,
        response_model=AcceptedModel,
        responses={"default": PROBLEM_RESPONSE},
    )
    def create_run(
        command: RunCreateModel,
        response: Response,
        idempotency_key: str = Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
        context: TrustedContext = Depends(require_scope("runs:write")),
        service: RunController = Depends(require_controller),
    ) -> AcceptedModel:
        accepted = service.admit_run(
            context,
            command.model_dump(mode="json", exclude_none=True),
            idempotency_key,
        )
        response.headers["Location"] = accepted.location
        response.headers["Retry-After"] = "1"
        return AcceptedModel(resource_id=accepted.resource_id, location=accepted.location)

    @app.get(
        "/v1/runs/{run_id}",
        operation_id="getRun",
        response_model=RunModel,
        responses={"default": PROBLEM_RESPONSE},
    )
    def get_run(
        run_id: str,
        context: TrustedContext = Depends(require_scope("runs:read")),
        service: RunController = Depends(require_controller),
    ) -> RunModel:
        snapshot = service.get_run(context, run_id)
        return RunModel.from_snapshot(snapshot, service.list_actions(context, run_id))

    @app.get(
        "/v1/runs/{run_id}/event-history",
        operation_id="getRunHistory",
        response_model=EventPageModel,
        responses={"default": PROBLEM_RESPONSE},
    )
    def get_run_history(
        run_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
        context: TrustedContext = Depends(require_scope("runs:read")),
        service: RunController = Depends(require_controller),
    ) -> EventPageModel:
        page = service.get_event_history(context, run_id, after=after, limit=limit)
        return EventPageModel(
            events=[EventModel.model_validate(event.public()) for event in page.events],
            next_after=page.next_after,
        )

    @app.get(
        "/v1/runs/{run_id}/events",
        operation_id="streamRunEvents",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "SSE stream of committed Run events",
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            },
            "default": PROBLEM_RESPONSE,
        },
    )
    def stream_run_events(
        run_id: str,
        last_event_id: str | None = Header(
            default=None,
            alias="Last-Event-ID",
            pattern=r"^[0-9]+$",
        ),
        context: TrustedContext = Depends(require_scope("runs:read")),
        service: RunController = Depends(require_controller),
    ) -> StreamingResponse:
        after = int(last_event_id) if last_event_id is not None else 0
        page = service.get_event_history(context, run_id, after=after, limit=1000)

        def committed_events() -> Any:
            for event in page.events:
                data = json.dumps(event.public(), ensure_ascii=False, separators=(",", ":"))
                yield f"id: {event.seq}\nevent: {event.type}\ndata: {data}\n\n"

        return StreamingResponse(
            committed_events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/v1/runs/{run_id}/cancellations",
        operation_id="requestRunCancellation",
        status_code=202,
        response_model=AcceptedModel,
        responses={"default": PROBLEM_RESPONSE},
    )
    def request_run_cancellation(
        run_id: str,
        command: CancelCreateModel,
        response: Response,
        idempotency_key: str = Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
        context: TrustedContext = Depends(require_scope("runs:write")),
        service: RunController = Depends(require_controller),
    ) -> AcceptedModel:
        accepted = service.request_cancellation(
            context,
            run_id,
            command.model_dump(mode="json", exclude_none=True),
            idempotency_key,
        )
        response.headers["Location"] = accepted.location
        response.headers["Retry-After"] = "1"
        return AcceptedModel(resource_id=accepted.resource_id, location=accepted.location)

    @app.get(
        "/v1/actions/{action_id}",
        operation_id="getAction",
        responses={"default": PROBLEM_RESPONSE},
    )
    def get_action(
        action_id: str,
        context: TrustedContext = Depends(require_scope("actions:read")),
        service: RunController = Depends(require_controller),
    ) -> dict[str, Any]:
        action = service.get_action(context, action_id)
        return {
            "id": action.id,
            "run_id": action.run_id,
            "status": action.status.value,
            "request_hash": action.request_hash,
            "capability_id": action.capability_id,
            "capability_revision": action.capability_revision,
            "effect_semantics": action.effect_semantics,
            "result": action.result,
        }

    @app.get(
        "/v1/input-requests/{input_request_id}",
        operation_id="getInputRequest",
        response_model=InputRequestModel,
        responses={"default": PROBLEM_RESPONSE},
    )
    def get_input_request(
        input_request_id: str,
        response: Response,
        context: TrustedContext = Depends(require_scope("inputs:read")),
        service: RunController = Depends(require_controller),
    ) -> InputRequestModel:
        request = service.get_input_request(context, input_request_id)
        response.headers["ETag"] = f'"{request.version}"'
        return InputRequestModel.model_validate(request.public())

    @app.post(
        "/v1/input-requests/{input_request_id}/responses",
        operation_id="respondToInput",
        status_code=202,
        response_model=AcceptedModel,
        responses={"default": PROBLEM_RESPONSE},
    )
    def respond_to_input(
        input_request_id: str,
        command: InputResponseModel,
        response: Response,
        idempotency_key: str = Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
        if_match: str = Header(alias="If-Match"),
        context: TrustedContext = Depends(require_scope("inputs:respond")),
        service: RunController = Depends(require_controller),
    ) -> AcceptedModel:
        version = _parse_version_etag(if_match)
        request = service.respond_to_input(
            context,
            input_request_id,
            expected_version=version,
            decision=command.decision,
            values=command.values,
            comment=command.comment,
            idempotency_key=idempotency_key,
        )
        location = f"/v1/input-requests/{request.id}"
        response.headers["Location"] = location
        response.headers["Retry-After"] = "1"
        return AcceptedModel(resource_id=request.id, location=location)

    @app.post(
        "/v1/actions/{action_id}/reconciliations",
        operation_id="reconcileAction",
        status_code=202,
        response_model=AcceptedModel,
        responses={"default": PROBLEM_RESPONSE},
    )
    def reconcile_action(
        action_id: str,
        command: ReconciliationModel,
        response: Response,
        idempotency_key: str = Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
        if_match: str = Header(alias="If-Match"),
        context: TrustedContext = Depends(require_scope("actions:reconcile")),
        service: RunController = Depends(require_controller),
    ) -> AcceptedModel:
        if command.action_id != action_id:
            from hnh.domain.errors import SchemaInvalid

            raise SchemaInvalid("body action_id must match the resource path")
        service.reconcile_action(
            context,
            action_id,
            expected_version=_parse_version_etag(if_match),
            resolution=command.resolution,
            evidence_ids=command.evidence_ids,
            comment=command.comment,
            idempotency_key=idempotency_key,
        )
        location = f"/v1/actions/{action_id}"
        response.headers["Location"] = location
        response.headers["Retry-After"] = "1"
        return AcceptedModel(resource_id=action_id, location=location)

    @app.post(
        "/v1/execution-sessions",
        operation_id="createExecutionSession",
        status_code=202,
        response_model=AcceptedModel,
        responses={"default": PROBLEM_RESPONSE},
    )
    def create_execution_session(
        command: SessionCreateModel,
        response: Response,
        idempotency_key: str = Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
        context: TrustedContext = Depends(require_scope("execution:write")),
        resource_dispatcher: ResourceDispatcher = Depends(require_dispatcher),
    ) -> AcceptedModel:
        result = resource_dispatcher.execute_effect(
            context,
            HttpOperation(
                method="POST",
                target="/v1/execution-sessions",
                headers={"content-type": "application/json"},
                payload={"kind": "json", "value": command.model_dump(mode="json")},
            ),
            idempotency_key=idempotency_key,
            transport_kind="http",
        )
        session_id = str(result.value["id"])
        location = f"/v1/execution-sessions/{session_id}"
        response.headers["Location"] = location
        response.headers["Retry-After"] = "1"
        return AcceptedModel(resource_id=session_id, location=location)

    @app.get(
        "/v1/execution-sessions/{session_id}",
        operation_id="getExecutionSession",
        response_model=ExecutionSessionModel,
        responses={"default": PROBLEM_RESPONSE},
    )
    def get_execution_session(
        session_id: str,
        context: TrustedContext = Depends(require_scope("execution:read")),
        service: ExecutionService = Depends(require_execution_service),
        action_gateway: ActionGateway = Depends(require_gateway),
    ) -> ExecutionSessionModel:
        action_gateway.bind(
            context,
            HttpOperation(method="GET", target=f"/v1/execution-sessions/{session_id}"),
        )
        return ExecutionSessionModel.model_validate(
            service.get_session(context, session_id).public()
        )

    @app.post(
        "/v1/execution-sessions/{session_id}/executions",
        operation_id="executePython",
        status_code=202,
        response_model=AcceptedModel,
        responses={"default": PROBLEM_RESPONSE},
    )
    def execute_python(
        session_id: str,
        response: Response,
        code: str = Body(media_type="text/plain", max_length=1048576),
        content_type: str = Header(alias="Content-Type"),
        idempotency_key: str = Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
        context: TrustedContext = Depends(require_scope("execution:write")),
        resource_dispatcher: ResourceDispatcher = Depends(require_dispatcher),
    ) -> AcceptedModel:
        if content_type.split(";", 1)[0].strip().lower() != "text/plain":
            raise UnsupportedMediaType()
        result = resource_dispatcher.execute_effect(
            context,
            HttpOperation(
                method="POST",
                target=f"/v1/execution-sessions/{session_id}/executions",
                headers={"content-type": "text/plain"},
                payload={"kind": "text", "text": code},
            ),
            idempotency_key=idempotency_key,
            transport_kind="http",
        )
        location = f"/v1/actions/{result.action_id}"
        response.headers["Location"] = location
        response.headers["Retry-After"] = "1"
        return AcceptedModel(resource_id=result.action_id, location=location)

    @app.get(
        "/v1/workspaces/{workspace_id}/files/{file_path:path}",
        operation_id="readWorkspaceFile",
        responses={"default": PROBLEM_RESPONSE},
    )
    def read_workspace_file(
        workspace_id: str,
        file_path: str,
        context: TrustedContext = Depends(require_scope("workspace:read")),
        resource_dispatcher: ResourceDispatcher = Depends(require_dispatcher),
    ) -> Response:
        content = resource_dispatcher.read_workspace_file(context, workspace_id, file_path)
        return Response(
            content=content.content,
            media_type=content.media_type,
            headers={"ETag": content.snapshot.etag},
        )

    @app.put(
        "/v1/workspaces/{workspace_id}/files/{file_path:path}",
        operation_id="writeWorkspaceFile",
        responses={"default": PROBLEM_RESPONSE},
    )
    def write_workspace_file(
        workspace_id: str,
        file_path: str,
        body: str = Body(media_type="text/plain", max_length=1048576),
        idempotency_key: str = Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
        if_match: str | None = Header(default=None, alias="If-Match"),
        if_none_match: str | None = Header(default=None, alias="If-None-Match"),
        context: TrustedContext = Depends(require_scope("workspace:write")),
        resource_dispatcher: ResourceDispatcher = Depends(require_dispatcher),
    ) -> JSONResponse:
        headers = {"content-type": "text/plain"}
        if if_match is not None:
            headers["if-match"] = if_match
        if if_none_match is not None:
            headers["if-none-match"] = if_none_match
        result = resource_dispatcher.execute_effect(
            context,
            HttpOperation(
                method="PUT",
                target=f"/v1/workspaces/{workspace_id}/files/{file_path}",
                headers=headers,
                payload={"kind": "text", "text": body},
            ),
            idempotency_key=idempotency_key,
            transport_kind="http",
        )
        return JSONResponse(
            status_code=result.status_code,
            content=result.value,
            headers={"ETag": str(result.value["etag"])},
        )

    @app.post(
        "/v1/artifacts",
        operation_id="createArtifact",
        responses={"default": PROBLEM_RESPONSE},
    )
    async def create_artifact(
        request: Request,
        idempotency_key: str = Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
        context: TrustedContext = Depends(require_scope("artifacts:write")),
        resource_dispatcher: ResourceDispatcher = Depends(require_dispatcher),
    ) -> JSONResponse:
        content = await request.body()
        media_type = request.headers.get("content-type", "application/octet-stream").split(";", 1)[
            0
        ]
        result = resource_dispatcher.execute_effect(
            context,
            HttpOperation(
                method="POST",
                target="/v1/artifacts",
                headers={"content-type": "application/octet-stream"},
                payload={
                    "kind": "json",
                    "value": {
                        "content_base64": base64.b64encode(content).decode(),
                        "media_type": media_type,
                    },
                },
            ),
            idempotency_key=idempotency_key,
            transport_kind="http",
        )
        return JSONResponse(status_code=result.status_code, content=result.value)

    @app.get(
        "/v1/artifacts/{artifact_id}",
        operation_id="getArtifact",
        responses={"default": PROBLEM_RESPONSE},
    )
    def get_artifact(
        artifact_id: str,
        context: TrustedContext = Depends(require_scope("artifacts:read")),
        resource_dispatcher: ResourceDispatcher = Depends(require_dispatcher),
    ) -> dict[str, Any]:
        return resource_dispatcher.get_artifact(context, artifact_id).public()

    @app.get(
        "/v1/artifacts/{artifact_id}/content",
        operation_id="getArtifactContent",
        responses={"default": PROBLEM_RESPONSE},
    )
    def get_artifact_content(
        artifact_id: str,
        context: TrustedContext = Depends(require_scope("artifacts:read")),
        resource_dispatcher: ResourceDispatcher = Depends(require_dispatcher),
    ) -> Response:
        content, media_type, etag = resource_dispatcher.get_artifact_content(context, artifact_id)
        return Response(content=content, media_type=media_type, headers={"ETag": etag})

    @app.post(
        "/v1/integrations/{integration_id}/operations/{operation_name}/invocations",
        operation_id="invokeIntegrationOperation",
        status_code=202,
        response_model=AcceptedModel,
        responses={"default": PROBLEM_RESPONSE},
    )
    def invoke_integration_operation(
        integration_id: str,
        operation_name: str,
        payload: dict[str, Any],
        response: Response,
        idempotency_key: str = Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
        context: TrustedContext = Depends(require_scope("integrations:invoke")),
        resource_dispatcher: ResourceDispatcher = Depends(require_dispatcher),
    ) -> AcceptedModel:
        scoped_dispatcher = scoped_integration_dispatcher(context)
        result = scoped_dispatcher.execute_effect(
            context,
            HttpOperation(
                method="POST",
                target=(
                    f"/v1/integrations/{integration_id}/operations/{operation_name}/invocations"
                ),
                headers={"content-type": "application/json"},
                payload={"kind": "json", "value": payload},
            ),
            idempotency_key=idempotency_key,
            transport_kind="http",
        )
        location = f"/v1/actions/{result.action_id}"
        response.headers["Location"] = location
        response.headers["Retry-After"] = "1"
        return AcceptedModel(resource_id=result.action_id, location=location)

    return app


app = create_app()
