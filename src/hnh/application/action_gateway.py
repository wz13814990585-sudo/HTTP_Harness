from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from sqlalchemy import Engine

from hnh.application.capabilities import CapabilityRegistry
from hnh.application.operations import (
    BoundOperation,
    HttpOperation,
    operation_fingerprint,
    payload_value,
    require_json_depth,
)
from hnh.application.resources import ResourceStore, normalize_file_path
from hnh.application.run_controller import (
    LEASE_REPLAYABLE_LOCAL_CAPABILITIES,
    ActionSnapshot,
    ClaimedJob,
    RunController,
)
from hnh.domain.errors import (
    HarnessError,
    InvalidOperation,
    LeaseDispatchUnsupported,
    PreconditionFailed,
    PreconditionRequired,
    SchemaInvalid,
)
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus

if TYPE_CHECKING:
    from hnh.application.execution import ExecutionService
    from hnh.ports.integrations import IntegrationExecution, IntegrationExecutor


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    action_id: str
    request_hash: str
    status_code: int
    value: dict[str, Any]
    replayed: bool
    succeeded: bool


class ActionGateway:
    """The sole binding/admission path for model and direct HTTP effects."""

    def __init__(
        self,
        engine: Engine,
        registry: CapabilityRegistry,
        resources: ResourceStore,
        controller: RunController,
        execution_service: ExecutionService | None = None,
        integration_executor: IntegrationExecutor | None = None,
    ) -> None:
        self._engine = engine
        self.registry = registry
        self.resources = resources
        self.controller = controller
        self.execution_service = execution_service
        self.integration_executor = integration_executor
        self.dispatch_count = 0

    def bind(
        self, context: TrustedContext, proposed: HttpOperation | dict[str, Any]
    ) -> BoundOperation:
        try:
            operation = (
                proposed
                if isinstance(proposed, HttpOperation)
                else HttpOperation.model_validate(proposed)
            )
        except ValueError as exc:
            raise SchemaInvalid(str(exc)) from exc
        capability, path_parameters = self.registry.resolve(
            context, operation.method, operation.target
        )
        if operation.query:
            raise InvalidOperation("this capability does not accept query parameters")
        supplied_type = operation.headers.get("content-type")
        if supplied_type is not None:
            base_type = supplied_type.split(";", 1)[0].strip().lower()
            if base_type not in capability.request_media_types:
                raise InvalidOperation("content-type is not supported by the capability")
        parameters = self._parameters(capability.id, path_parameters, operation)
        require_json_depth(parameters)
        validator = Draft202012Validator(capability.input_schema)
        error = next(iter(sorted(validator.iter_errors(parameters), key=self._error_key)), None)
        if error is not None:
            location = "/".join(str(item) for item in error.absolute_path) or "$"
            raise SchemaInvalid(f"{capability.id} input at {location}: {error.message}")
        return BoundOperation(capability, parameters, operation_fingerprint(operation, capability))

    def execute(
        self,
        context: TrustedContext,
        proposed: HttpOperation | dict[str, Any],
        *,
        idempotency_key: str,
        transport_kind: str,
        run_id: str | None = None,
        turn_id: int = 0,
        call_index: int = 0,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> ExecutionResult:
        if claimed_job is not None and run_id is None:
            raise InvalidOperation("a leased worker action requires an explicit Run")
        if run_id is not None:
            context = self.controller.effective_context(context, run_id)
        bound = self.bind(context, proposed)
        if (
            claimed_job is not None
            and bound.capability.effect_semantics != "read_only"
            and not (
                bound.capability.effect_semantics == "local_transactional"
                and bound.capability.id in LEASE_REPLAYABLE_LOCAL_CAPABILITIES
            )
        ):
            # Only native writes with a transactional PostgreSQL effect ledger
            # and a stable Action business key can be reclaimed after a crash.
            raise LeaseDispatchUnsupported(
                "lease-bound dispatch is not ready for this effect class"
            )
        if (
            claimed_job is not None
            and self.integration_executor is not None
            and self.integration_executor.handles(bound.capability.id)
        ):
            raise LeaseDispatchUnsupported("lease-bound MCP dispatch is not yet supported")
        implicit_run = run_id is None
        action_run_id = run_id
        if action_run_id is None:
            key_material = f"{bound.capability.id}\0{idempotency_key}".encode()
            internal_key = f"operation:{hashlib.sha256(key_material).hexdigest()}"
            accepted = self.controller.admit_run(
                context,
                {
                    "agent_id": "system.http-resource",
                    "input": f"Execute {bound.capability.id}",
                    "limits": {"max_tool_calls": 1},
                },
                internal_key,
            )
            action_run_id = accepted.resource_id
        admission = self.controller.get_or_record_admitted_action(
            context,
            action_run_id,
            turn_id=turn_id,
            call_index=call_index,
            request_hash=bound.request_hash,
            capability_id=bound.capability.id,
            capability_revision=bound.capability.revision,
            effect_semantics=bound.capability.effect_semantics,
            bound_operation=bound.parameters,
            claimed_job=claimed_job,
            worker_id=worker_id,
        )
        action = admission.action
        local_effect_key = (
            f"action:{action.id}"
            if claimed_job is not None
            and bound.capability.effect_semantics == "local_transactional"
            and bound.capability.id in LEASE_REPLAYABLE_LOCAL_CAPABILITIES
            else None
        )
        if action.status == ActionStatus.SUCCEEDED:
            return self._from_snapshot(action, True)
        if action.status == ActionStatus.FAILED:
            if action.result is not None and "value" in action.result:
                return self._from_snapshot(action, True)
            self._raise_stored_failure(action)
        if action.status == ActionStatus.OUTCOME_UNKNOWN:
            return ExecutionResult(
                action.id,
                action.request_hash,
                202,
                {"result_type": "outcome_unknown", "action_id": action.id},
                True,
                False,
            )
        if bound.capability.requires_approval:
            if action.approval_request_id is None:
                if implicit_run:
                    self.controller.ensure_run_running(action_run_id, actor_ref="action_gateway")
                request = self.controller.request_approval(
                    context,
                    action.id,
                    prompt=f"Approve {bound.capability.id}",
                    target_summary=f"{bound.capability.method} {bound.capability.path_template}",
                    expires_at=datetime.now(UTC) + timedelta(minutes=15),
                )
                return ExecutionResult(
                    action.id,
                    action.request_hash,
                    202,
                    {
                        "input_request_id": request.id,
                        "location": f"/v1/input-requests/{request.id}",
                    },
                    admission.replayed,
                    False,
                )
            if action.status == ActionStatus.WAITING_INPUT:
                request = self.controller.get_input_request(context, action.approval_request_id)
                return ExecutionResult(
                    action.id,
                    action.request_hash,
                    202,
                    {
                        "input_request_id": request.id,
                        "location": f"/v1/input-requests/{request.id}",
                    },
                    True,
                    False,
                )
            self.controller.validate_approval(context, action.id)
        if self.integration_executor is not None and self.integration_executor.handles(
            bound.capability.id
        ):
            if action.status in {ActionStatus.WAITING_INPUT, ActionStatus.WAITING_EXTERNAL}:
                pending = self.integration_executor.pending(context, action.id)
                return ExecutionResult(
                    action.id,
                    action.request_hash,
                    pending.status_code,
                    pending.value,
                    True,
                    False,
                )
            if action.status == ActionStatus.READY:
                self.controller.validate_dispatch(
                    context,
                    action.id,
                    capability_revision=bound.capability.revision,
                )
                resumed = self.integration_executor.resume_if_ready(context, action.id)
                if resumed is not None:
                    return ExecutionResult(
                        action.id,
                        action.request_hash,
                        resumed.status_code,
                        resumed.value,
                        True,
                        resumed.succeeded,
                    )
        self.controller.ensure_run_running(action_run_id, actor_ref="action_gateway")
        self.controller.validate_dispatch(
            context,
            action.id,
            capability_revision=bound.capability.revision,
        )
        if not self.controller.begin_action_dispatch(
            action.id,
            claimed_job=claimed_job,
            worker_id=worker_id,
            downstream_idempotency_key=local_effect_key,
        ):
            current = self.controller.get_action(context, action.id)
            if current.status == ActionStatus.SUCCEEDED:
                return self._from_snapshot(current, True)
            if current.status == ActionStatus.FAILED:
                self._raise_stored_failure(current)
            raise InvalidOperation("the admitted action is already executing")
        self.dispatch_count += 1
        try:
            outcome = self.execute_bound(
                context,
                action.id,
                bound,
                idempotency_key=local_effect_key or idempotency_key,
            )
        except HarnessError as exc:
            self.controller.complete_action(
                action.id,
                result={
                    "error": {
                        "status": exc.status,
                        "code": exc.code,
                        "title": exc.title,
                        "detail": exc.detail,
                        "retry_hint": exc.retry_hint,
                    }
                },
                succeeded=False,
                transport_kind=transport_kind,
                response_status=exc.status,
                claimed_job=claimed_job,
                worker_id=worker_id,
            )
            if implicit_run:
                self.controller.finish_implicit_run(
                    action_run_id,
                    succeeded=False,
                    actor_ref="action_gateway",
                )
            raise
        if not isinstance(outcome, tuple):
            if not outcome.terminal:
                return ExecutionResult(
                    action.id,
                    action.request_hash,
                    outcome.status_code,
                    outcome.value,
                    admission.replayed,
                    False,
                )
            if outcome.committed:
                completed = self.controller.get_action(context, action.id)
                return self._from_snapshot(completed, admission.replayed)
            value = outcome.value
            status_code = outcome.status_code
            application_succeeded = outcome.succeeded
        else:
            value, status_code, application_succeeded = outcome
        completed = self.controller.complete_action(
            action.id,
            result={"status_code": status_code, "value": value},
            succeeded=application_succeeded,
            transport_kind=transport_kind,
            response_status=status_code,
            claimed_job=claimed_job,
            worker_id=worker_id,
        )
        if implicit_run:
            self.controller.finish_implicit_run(
                action_run_id,
                succeeded=application_succeeded,
                actor_ref="action_gateway",
            )
        return self._from_snapshot(completed, admission.replayed)

    def execute_bound(
        self,
        context: TrustedContext,
        action_id: str,
        bound: BoundOperation,
        *,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], int, bool] | IntegrationExecution:
        """Execute an already admitted operation; this never calls a public HTTP route."""

        parameters = bound.parameters
        if bound.capability.id == "workspace.file.read":
            file_content = self.resources.read_file(
                context,
                str(parameters["workspace_id"]),
                str(parameters["file_path"]),
            )
            if file_content.media_type.startswith("text/"):
                return (
                    {
                        **file_content.snapshot.public(),
                        "media_type": file_content.media_type,
                        "text": file_content.content.decode("utf-8", errors="replace"),
                    },
                    200,
                    True,
                )
            return (
                {
                    **file_content.snapshot.public(),
                    "media_type": file_content.media_type,
                    "content_base64": base64.b64encode(file_content.content).decode(),
                },
                200,
                True,
            )
        if bound.capability.id == "artifact.read":
            return (
                self.resources.get_artifact(context, str(parameters["artifact_id"])).public(),
                200,
                True,
            )
        if bound.capability.id == "artifact.content.read":
            artifact_bytes, media_type, etag = self.resources.get_artifact_content(
                context, str(parameters["artifact_id"])
            )
            return (
                {
                    "artifact_id": str(parameters["artifact_id"]),
                    "media_type": media_type,
                    "etag": etag,
                    "content_base64": base64.b64encode(artifact_bytes).decode(),
                },
                200,
                True,
            )
        if bound.capability.id == "native.echo":
            return {"message": str(parameters["message"])}, 200, True
        if bound.capability.id == "workspace.file.write":
            snapshot, created = self.resources.write_file(
                context,
                str(parameters["workspace_id"]),
                str(parameters["file_path"]),
                str(parameters["text"]).encode(),
                idempotency_key,
                if_match=parameters.get("if_match"),
                if_none_match=parameters.get("if_none_match"),
                source_action_id=action_id,
            )
            return snapshot.public(), 201 if created else 200, True
        if bound.capability.id == "artifact.create":
            try:
                artifact_bytes = base64.b64decode(str(parameters["content_base64"]), validate=True)
            except ValueError as exc:
                raise SchemaInvalid("content_base64 is not valid base64") from exc
            artifact = self.resources.create_artifact(
                context,
                artifact_bytes,
                str(parameters["media_type"]),
                idempotency_key,
                source_action_id=action_id,
            )
            return artifact.public(), 201, True
        if bound.capability.id == "execution.session.create":
            if self.execution_service is None:
                from hnh.domain.errors import ExecutionUnavailable

                raise ExecutionUnavailable("No isolated execution broker is configured")
            execution_session = self.execution_service.create_session(
                context,
                str(parameters["run_id"]),
                runtime=str(parameters["runtime"]),
                profile_id=str(parameters["profile_id"]),
                source_action_id=action_id,
            )
            return execution_session.public(), 201, True
        if bound.capability.id == "execution.python":
            if self.execution_service is None:
                from hnh.domain.errors import ExecutionUnavailable

                raise ExecutionUnavailable("No isolated execution broker is configured")
            outcome = self.execution_service.execute_cell(
                context,
                str(parameters["session_id"]),
                str(parameters["code"]),
                action_id=action_id,
                request_hash=bound.request_hash,
            )
            return outcome.receipt, 200, outcome.succeeded
        if self.integration_executor is not None and self.integration_executor.handles(
            bound.capability.id
        ):
            return self.integration_executor.invoke(
                context,
                action_id,
                bound,
                idempotency_key=idempotency_key,
            )
        raise InvalidOperation("capability has no executable adapter")

    @staticmethod
    def _parameters(
        capability_id: str,
        path_parameters: dict[str, str],
        operation: HttpOperation,
    ) -> dict[str, Any]:
        if "file_path" in path_parameters:
            path_parameters["file_path"] = normalize_file_path(path_parameters["file_path"])
        value = payload_value(operation)
        if capability_id == "native.echo":
            if not isinstance(value, dict):
                raise SchemaInvalid("native.echo requires a JSON object payload")
            return dict(value)
        if capability_id == "workspace.file.write":
            result: dict[str, Any] = dict(path_parameters)
            result["text"] = value
            has_match = "if-match" in operation.headers
            has_none_match = "if-none-match" in operation.headers
            if not has_match and not has_none_match:
                raise PreconditionRequired()
            if has_match and has_none_match:
                raise InvalidOperation("If-Match and If-None-Match are mutually exclusive")
            if has_none_match and operation.headers["if-none-match"] != "*":
                raise PreconditionFailed()
            if has_match:
                result["if_match"] = operation.headers["if-match"]
            if has_none_match:
                result["if_none_match"] = operation.headers["if-none-match"]
            return result
        if capability_id == "artifact.create":
            if not isinstance(value, dict):
                raise SchemaInvalid("artifact.create requires a JSON object payload")
            return dict(value)
        if capability_id == "execution.session.create":
            if not isinstance(value, dict):
                raise SchemaInvalid("execution.session.create requires a JSON object payload")
            return dict(value)
        if capability_id == "execution.python":
            if not isinstance(value, str):
                raise SchemaInvalid("execution.python requires a text payload")
            return {**path_parameters, "code": value}
        if capability_id.startswith("mcp."):
            if not isinstance(value, dict):
                raise SchemaInvalid("MCP capabilities require a JSON object payload")
            return dict(value)
        return dict(path_parameters)

    @staticmethod
    def _error_key(error: ValidationError) -> tuple[str, str]:
        return ("/".join(str(item) for item in error.absolute_path), error.message)

    @staticmethod
    def _from_snapshot(action: ActionSnapshot, replayed: bool) -> ExecutionResult:
        if action.result is None:
            raise RuntimeError("terminal action is missing its result")
        return ExecutionResult(
            action.id,
            action.request_hash,
            int(action.result["status_code"]),
            dict(action.result["value"]),
            replayed,
            action.status == ActionStatus.SUCCEEDED,
        )

    @staticmethod
    def _raise_stored_failure(action: ActionSnapshot) -> None:
        if action.result is None or "error" not in action.result:
            raise RuntimeError("failed action is missing structured error evidence")
        value = action.result["error"]
        raise HarnessError(
            int(value["status"]),
            str(value["code"]),
            str(value["title"]),
            None if value.get("detail") is None else str(value["detail"]),
            str(value.get("retry_hint", "never")),
        )
