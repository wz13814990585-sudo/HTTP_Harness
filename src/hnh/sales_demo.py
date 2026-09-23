"""End-to-end sales report, approval, response-loss, and reconciliation demo.

This module is intentionally a trusted demo orchestrator rather than another
general worker.  Every business effect is still bound and admitted through the
same ActionGateway, while RunController remains the only state owner.
"""

from __future__ import annotations

import json
import socket
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import Engine

from hnh.adapters.blobs.filesystem import FileBlobStore
from hnh.application.action_gateway import ActionGateway
from hnh.application.capabilities import Capability, CapabilityRegistry, builtin_capabilities
from hnh.application.completion import CompletionGate
from hnh.application.execution import ExecutionService
from hnh.application.operations import BoundOperation, HttpOperation
from hnh.application.recovery import RecoveryCoordinator
from hnh.application.resources import ResourceStore
from hnh.application.run_controller import RunController
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus, RunStatus
from hnh.ports.effects import (
    AmbiguousDispatch,
    EffectCancellation,
    EffectDispatchResult,
    EffectReconciliation,
)
from hnh.ports.execution import ExecutionBroker
from hnh.ports.integrations import IntegrationExecution
from hnh.ports.models import FinalCandidate

SALES_CSV = """date,region,product,units,unit_price
2026-08-01,North,Widget A,10,120.00
2026-08-03,North,Widget B,5,200.00
2026-08-07,South,Widget A,8,120.00
2026-08-11,South,Widget C,7,150.00
2026-08-16,East,Widget B,6,200.00
2026-08-22,East,Widget C,4,150.00
"""

EXPECTED_REPORT = """# Monthly Sales Summary

- Source rows: 6
- Total units: 40
- Total revenue: $6,010.00

| Region | Units | Revenue |
|---|---:|---:|
| North | 15 | $2,200.00 |
| South | 15 | $2,010.00 |
| East | 10 | $1,800.00 |
"""

DEMO_SCOPES = frozenset(
    {
        "actions:read",
        "actions:reconcile",
        "artifacts:read",
        "artifacts:write",
        "execution:read",
        "execution:write",
        "inputs:read",
        "inputs:respond",
        "integrations:invoke",
        "runs:read",
        "runs:write",
        "workspace:read",
        "workspace:write",
    }
)
DEMO_CONTEXT = TrustedContext("portfolio-local", "sales-demo-operator", DEMO_SCOPES)


class SalesDemoError(RuntimeError):
    """A safe, operator-facing demo failure."""


class SalesDemoDeclined(SalesDemoError):
    """The operator deliberately denied publication."""


@dataclass(frozen=True, slots=True)
class ApprovalView:
    input_request_id: str
    action_id: str
    artifact_id: str
    artifact_sha256: str
    target_summary: str | None


@dataclass(slots=True)
class PublicationLedger:
    publications: list[dict[str, Any]] = field(default_factory=list)
    requests: int = 0
    queries: int = 0
    _lock: Lock = field(default_factory=Lock)

    def publish(self, action_id: str, payload: dict[str, Any]) -> tuple[str, bool]:
        with self._lock:
            self.requests += 1
            handle = f"publication-{len(self.publications) + 1}"
            self.publications.append(
                {"action_id": action_id, "payload": dict(payload), "handle": handle}
            )
            return handle, self.requests == 1

    def get(self, action_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self.queries += 1
            return [dict(item) for item in self.publications if item["action_id"] == action_id]


class PublicationSimulator:
    """Independent local HTTP ledger that drops the first committed response."""

    def __init__(self) -> None:
        self.ledger = PublicationLedger()
        ledger = self.ledger

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path != "/publish":
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 65536:
                        raise ValueError
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict) or not isinstance(body.get("action_id"), str):
                        raise ValueError
                    payload = body.get("payload")
                    if not isinstance(payload, dict):
                        raise ValueError
                except (ValueError, json.JSONDecodeError):
                    self.send_error(400)
                    return
                handle, drop_response = ledger.publish(body["action_id"], payload)
                if drop_response:
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                self._json(201, {"handle": handle, "applied": True})

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                if not parsed.path.startswith("/effects/"):
                    self.send_error(404)
                    return
                action_id = parsed.path.removeprefix("/effects/")
                self._json(200, {"publications": ledger.get(action_id)})

            def _json(self, status: int, value: dict[str, Any]) -> None:
                encoded = json.dumps(value, sort_keys=True).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        port = int(self._server.server_address[1])
        return f"http://127.0.0.1:{port}"

    def __enter__(self) -> PublicationSimulator:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class PublicationDriver:
    """Demo-only registered adapter for the independent publication ledger."""

    def __init__(self, base_url: str) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=2.0,
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def dispatch(
        self,
        action_id: str,
        operation: dict[str, Any],
        downstream_idempotency_key: str | None,
    ) -> EffectDispatchResult:
        del downstream_idempotency_key
        try:
            response = self._client.post(
                "/publish",
                json={"action_id": action_id, "payload": operation},
            )
        except httpx.HTTPError as exc:
            raise AmbiguousDispatch(
                {
                    "transport_error": type(exc).__name__,
                    "possible_dispatch": True,
                }
            ) from exc
        value = response.json()
        return EffectDispatchResult(
            response.status_code,
            value,
            applied=value.get("applied") is True,
            upstream_handle=value.get("handle"),
        )

    def reconcile(
        self,
        action_id: str,
        upstream_handle: str | None,
        downstream_idempotency_key: str | None,
    ) -> EffectReconciliation | None:
        del downstream_idempotency_key
        response = self._client.get(f"/effects/{quote(action_id, safe='')}")
        response.raise_for_status()
        publications = response.json()["publications"]
        if len(publications) != 1:
            return None
        handle = publications[0]["handle"]
        if upstream_handle is not None and upstream_handle != handle:
            return None
        return EffectReconciliation(
            "confirmed_applied",
            (f"remote-receipt:{handle}",),
            "independent publication ledger confirms exactly one publication",
        )

    def cancel(self, action_id: str, upstream_handle: str | None) -> EffectCancellation:
        del action_id, upstream_handle
        return EffectCancellation(False, False, {"reason": "demo publisher cannot cancel"})


class PublicationExecutor:
    """ActionGateway adapter that preserves ambiguous unsafe effects as unknown."""

    CAPABILITY_ID = "demo.report.publish"

    def __init__(self, controller: RunController, driver: PublicationDriver) -> None:
        self._controller = controller
        self._driver = driver

    def handles(self, capability_id: str) -> bool:
        return capability_id == self.CAPABILITY_ID

    def pending(self, context: TrustedContext, action_id: str) -> IntegrationExecution:
        action = self._controller.get_action(context, action_id)
        return IntegrationExecution(
            {"result_type": action.status.value, "action_id": action.id},
            202,
            False,
            False,
        )

    def resume_if_ready(
        self, context: TrustedContext, action_id: str
    ) -> IntegrationExecution | None:
        del context, action_id
        return None

    def invoke(
        self,
        context: TrustedContext,
        action_id: str,
        bound: BoundOperation,
        *,
        idempotency_key: str,
    ) -> IntegrationExecution:
        del context, idempotency_key
        try:
            result = self._driver.dispatch(action_id, bound.parameters, None)
        except AmbiguousDispatch as exc:
            self._controller.mark_action_outcome_unknown(
                action_id,
                receipt=exc.receipt,
                upstream_handle=exc.upstream_handle,
            )
            return IntegrationExecution(
                {"result_type": "outcome_unknown", "action_id": action_id},
                202,
                False,
                False,
            )
        return IntegrationExecution(
            result.body,
            result.status_code,
            200 <= result.status_code < 300 and result.applied is True,
        )


def publication_capability() -> Capability:
    return Capability(
        PublicationExecutor.CAPABILITY_ID,
        "1",
        "Publish one immutable Markdown report to the registered demo publisher.",
        "POST",
        "/v1/integrations/demo-publisher/operations/publish/invocations",
        ("application/json",),
        {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "artifact_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
            },
            "required": ["artifact_id", "artifact_sha256", "title"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"handle": {"type": "string"}},
        },
        "unsafe",
        10,
        True,
        ("complete", "outcome_unknown"),
        "integrations:invoke",
    )


def report_python(csv_text: str) -> str:
    """Return the deterministic sandbox cell used for the report calculation."""

    return (
        "csv_text = "
        + repr(csv_text)
        + "\n"
        + textwrap.dedent(
            """
        import csv
        import io
        import json
        from collections import defaultdict
        from decimal import Decimal
        from pathlib import Path

        rows = list(csv.DictReader(io.StringIO(csv_text)))
        regional = defaultdict(lambda: {"units": 0, "revenue": Decimal("0")})
        total_units = 0
        total_revenue = Decimal("0")
        for row in rows:
            units = int(row["units"])
            revenue = Decimal(row["unit_price"]) * units
            regional[row["region"]]["units"] += units
            regional[row["region"]]["revenue"] += revenue
            total_units += units
            total_revenue += revenue

        def money(value):
            return f"${value:,.2f}"

        lines = [
            "# Monthly Sales Summary",
            "",
            f"- Source rows: {len(rows)}",
            f"- Total units: {total_units}",
            f"- Total revenue: {money(total_revenue)}",
            "",
            "| Region | Units | Revenue |",
            "|---|---:|---:|",
        ]
        for region, values in sorted(
            regional.items(), key=lambda item: item[1]["revenue"], reverse=True
        ):
            lines.append(f"| {region} | {values['units']} | {money(values['revenue'])} |")
        report = "\\n".join(lines) + "\\n"
        output = Path("/workspace/outputs")
        output.mkdir(exist_ok=True)
        (output / "sales-report.md").write_text(report, encoding="utf-8")
        print(json.dumps({
            "source_rows": len(rows),
            "total_units": total_units,
            "total_revenue": format(total_revenue, ".2f"),
        }, sort_keys=True))
        """
        )
    )


def _operation(method: str, target: str, value: dict[str, Any]) -> HttpOperation:
    return HttpOperation(
        method=method,  # type: ignore[arg-type]
        target=target,
        headers={"content-type": "application/json"},
        payload={"kind": "json", "value": value},
    )


def run_sales_demo(
    engine: Engine,
    blob_root: Path,
    broker: ExecutionBroker,
    *,
    profile_id: str,
    approve: Callable[[ApprovalView], bool],
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Execute and independently verify the complete sales publication workflow."""

    announce = progress or (lambda _message: None)
    identifier = uuid4().hex[:12]
    workspace_id = f"sales-{identifier}"
    controller = RunController(engine)
    resources = ResourceStore(engine, FileBlobStore(blob_root))
    execution = ExecutionService(controller, resources, broker)

    with PublicationSimulator() as simulator:
        driver = PublicationDriver(simulator.base_url)
        executor = PublicationExecutor(controller, driver)
        registry = CapabilityRegistry((*builtin_capabilities(), publication_capability()))
        gateway = ActionGateway(
            engine,
            registry,
            resources,
            controller,
            execution,
            integration_executor=executor,
        )
        try:
            announce("[1/8] Generating and storing a deterministic sales CSV")
            gateway.execute(
                DEMO_CONTEXT,
                HttpOperation(
                    method="PUT",
                    target=f"/v1/workspaces/{workspace_id}/files/sales.csv",
                    headers={"content-type": "text/plain", "if-none-match": "*"},
                    payload={"kind": "text", "text": SALES_CSV},
                ),
                idempotency_key=f"sales-seed-{identifier}",
                transport_kind="inproc",
            )

            accepted = controller.admit_run(
                DEMO_CONTEXT,
                {
                    "agent_id": "sales-report-demo",
                    "input": (
                        "Read the managed sales CSV, calculate the summary in isolated Python, "
                        "create a Markdown Artifact, obtain approval, publish once, and reconcile "
                        "any unknown external outcome before completion."
                    ),
                    "workspace_id": workspace_id,
                    "limits": {"max_model_turns": 10, "max_tool_calls": 10},
                },
                f"sales-run-{identifier}",
            )
            run_id = accepted.resource_id
            announce(f"[2/8] Durable Run admitted: {run_id}")

            read = gateway.execute(
                DEMO_CONTEXT,
                HttpOperation(
                    method="GET",
                    target=f"/v1/workspaces/{workspace_id}/files/sales.csv",
                ),
                idempotency_key=f"sales-read-{identifier}",
                transport_kind="inproc",
                run_id=run_id,
                turn_id=1,
                call_index=0,
            )
            csv_text = str(read.value["text"])
            if csv_text != SALES_CSV:
                raise SalesDemoError("managed CSV bytes did not match the generated fixture")
            announce(f"[3/8] CSV read committed as Action {read.action_id}")

            session = gateway.execute(
                DEMO_CONTEXT,
                _operation(
                    "POST",
                    "/v1/execution-sessions",
                    {"run_id": run_id, "runtime": "python", "profile_id": profile_id},
                ),
                idempotency_key=f"sales-session-{identifier}",
                transport_kind="inproc",
                run_id=run_id,
                turn_id=2,
                call_index=0,
            )
            session_id = str(session.value["id"])
            calculated = gateway.execute(
                DEMO_CONTEXT,
                HttpOperation(
                    method="POST",
                    target=f"/v1/execution-sessions/{session_id}/executions",
                    headers={"content-type": "text/plain"},
                    payload={"kind": "text", "text": report_python(csv_text)},
                ),
                idempotency_key=f"sales-calculate-{identifier}",
                transport_kind="inproc",
                run_id=run_id,
                turn_id=3,
                call_index=0,
            )
            if not calculated.succeeded:
                raise SalesDemoError("isolated Python calculation failed")
            calculation = json.loads(str(calculated.value["stdout"]))
            exported = calculated.value.get("artifacts", [])
            if not isinstance(exported, list) or len(exported) != 1:
                raise SalesDemoError("Python calculation did not export exactly one report")
            artifact_id = str(exported[0]["artifact_id"])
            report_bytes, media_type, _etag = resources.get_artifact_content(
                DEMO_CONTEXT, artifact_id
            )
            artifact = resources.get_artifact(DEMO_CONTEXT, artifact_id)
            report_matches = (
                report_bytes == EXPECTED_REPORT.encode() and media_type == "text/markdown"
            )
            if not report_matches:
                raise SalesDemoError("Markdown Artifact failed independent byte verification")
            announce(
                "[4/8] Isolated Python created a verified Markdown Artifact "
                f"{artifact_id} (${calculation['total_revenue']})"
            )

            publish_operation = _operation(
                "POST",
                "/v1/integrations/demo-publisher/operations/publish/invocations",
                {
                    "artifact_id": artifact_id,
                    "artifact_sha256": artifact.sha256,
                    "title": "Monthly Sales Summary",
                },
            )
            proposed = gateway.execute(
                DEMO_CONTEXT,
                publish_operation,
                idempotency_key=f"sales-publish-{identifier}",
                transport_kind="inproc",
                run_id=run_id,
                turn_id=4,
                call_index=0,
            )
            request_id = str(proposed.value["input_request_id"])
            request = controller.get_input_request(DEMO_CONTEXT, request_id)
            if controller.get_run(DEMO_CONTEXT, run_id).status != RunStatus.WAITING_INPUT:
                raise SalesDemoError("Run did not enter waiting_input before publication")
            announce(f"[5/8] Run is waiting for exact-action approval: {request_id}")
            approved = approve(
                ApprovalView(
                    request_id,
                    proposed.action_id,
                    artifact_id,
                    artifact.sha256,
                    request.target_summary,
                )
            )
            answered_request = controller.respond_to_input(
                DEMO_CONTEXT,
                request.id,
                expected_version=request.version,
                decision="approve" if approved else "deny",
                values=None,
                comment="approved in sales demo" if approved else "denied in sales demo",
                idempotency_key=f"sales-approval-{identifier}",
            )
            if not approved:
                raise SalesDemoDeclined("publication was denied; no external request was sent")

            unknown = gateway.execute(
                DEMO_CONTEXT,
                publish_operation,
                idempotency_key=f"sales-publish-{identifier}",
                transport_kind="inproc",
                run_id=run_id,
                turn_id=4,
                call_index=0,
            )
            action_after_loss = controller.get_action(DEMO_CONTEXT, unknown.action_id)
            run_after_loss = controller.get_run(DEMO_CONTEXT, run_id)
            if (
                action_after_loss.status != ActionStatus.OUTCOME_UNKNOWN
                or run_after_loss.status != RunStatus.BLOCKED
            ):
                raise SalesDemoError("lost response did not preserve an unknown external outcome")
            announce("[6/8] Publisher committed, response was lost; Run is blocked, not retried")

            recovery = RecoveryCoordinator(controller)
            reconciled = recovery.reconcile(
                DEMO_CONTEXT,
                unknown.action_id,
                driver,
                idempotency_key=f"sales-reconcile-{identifier}",
            )
            if reconciled.action.status != ActionStatus.SUCCEEDED:
                raise SalesDemoError("publication reconciliation did not confirm the effect")
            announce("[7/8] Downstream query confirmed the publication without a second POST")

            replay = gateway.execute(
                DEMO_CONTEXT,
                publish_operation,
                idempotency_key=f"sales-publish-{identifier}",
                transport_kind="inproc",
                run_id=run_id,
                turn_id=4,
                call_index=0,
            )
            publication_count = len(simulator.ledger.publications)
            exactly_once_observed = (
                replay.replayed
                and replay.succeeded
                and simulator.ledger.requests == 1
                and publication_count == 1
            )
            if not exactly_once_observed:
                raise SalesDemoError("the replay guard did not preserve one observed publication")

            controller.ensure_run_running(run_id, actor_ref="sales_demo")
            gate = CompletionGate(engine, controller).verify(
                DEMO_CONTEXT,
                run_id,
                FinalCandidate(
                    answer="Sales report was approved, published, and reconciled exactly once.",
                    artifact_ids=[artifact_id],
                    evidence_ids=[read.action_id, calculated.action_id, unknown.action_id],
                    acceptance_claims=[
                        "CSV was read before calculation",
                        "Markdown report bytes match the independent oracle",
                        "Publication was explicitly approved",
                        "Response loss was reconciled without redispatch",
                    ],
                ),
                acceptance_results=[
                    {"name": "report_bytes", "passed": report_matches},
                    {
                        "name": "approval_recorded",
                        "passed": answered_request.status == "answered",
                    },
                    {"name": "one_publication", "passed": exactly_once_observed},
                    {
                        "name": "reconciliation",
                        "passed": reconciled.decision == "confirmed_applied",
                    },
                ],
            )
            final_run = controller.get_run(DEMO_CONTEXT, run_id)
            if not gate.verified or final_run.status != RunStatus.SUCCEEDED:
                raise SalesDemoError("CompletionGate did not verify the business workflow")
            events = controller.get_recent_events(DEMO_CONTEXT, run_id, limit=100)
            announce("[8/8] CompletionGate verified the Artifact and all committed evidence")
            return {
                "verified": True,
                "run_id": run_id,
                "run_status": final_run.status.value,
                "workspace_id": workspace_id,
                "csv_rows": calculation["source_rows"],
                "total_units": calculation["total_units"],
                "total_revenue": calculation["total_revenue"],
                "report_artifact_id": artifact_id,
                "report_sha256": artifact.sha256,
                "report_media_type": media_type,
                "approval_request_id": request_id,
                "approval_status": answered_request.status,
                "status_after_response_loss": run_after_loss.status.value,
                "action_status_after_response_loss": action_after_loss.status.value,
                "reconciliation": reconciled.decision,
                "publish_requests": simulator.ledger.requests,
                "publication_count": publication_count,
                "replay_without_redispatch": replay.replayed,
                "action_ids": [
                    read.action_id,
                    session.action_id,
                    calculated.action_id,
                    unknown.action_id,
                ],
                "event_count": len(events),
                "event_types": [event.type for event in events],
                "report_markdown": report_bytes.decode(),
            }
        finally:
            driver.close()
            execution.close()
