from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from hnh.application.mcp_catalog import MCPIntegrationBinding
from hnh.application.operations import BoundOperation
from hnh.application.run_controller import ClaimedJob, RunController
from hnh.domain.errors import HarnessError, InvalidOperation, ResourceNotFound, VersionConflict
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus, RunStatus
from hnh.ports.integrations import IntegrationExecution
from hnh.ports.mcp import (
    MCPClient,
    MCPComplete,
    MCPInputRequired,
    MCPInvocation,
    MCPProtocolFault,
    MCPTask,
)


class MCPActionCoordinator:
    """Maps MCP outcomes into durable kernel state without owning that state."""

    def __init__(
        self,
        controller: RunController,
        integrations: dict[str, tuple[MCPIntegrationBinding, MCPClient]],
        *,
        input_timeout: timedelta = timedelta(hours=1),
    ) -> None:
        self._controller = controller
        self._integrations = dict(integrations)
        self._input_timeout = input_timeout

    def handles(self, capability_id: str) -> bool:
        try:
            integration_id, _ = self._parse_capability(capability_id)
        except InvalidOperation:
            return False
        return integration_id in self._integrations

    def pending(self, context: TrustedContext, action_id: str) -> IntegrationExecution:
        try:
            task = self._controller.get_mcp_task(context, action_id)
        except ResourceNotFound:
            continuation = self._controller.get_mcp_continuation(context, action_id)
            return IntegrationExecution(
                {
                    "result_type": "input_required",
                    "input_request_id": continuation.input_request_id,
                    "location": f"/v1/input-requests/{continuation.input_request_id}",
                },
                202,
                False,
                False,
            )
        if task.status == "input_required":
            request = self._controller.get_latest_external_input_request(context, action_id)
            return IntegrationExecution(
                {
                    "result_type": "input_required",
                    "task_id": task.task_id,
                    "input_request_id": request.id,
                    "location": f"/v1/input-requests/{request.id}",
                },
                202,
                False,
                False,
            )
        return self._task_pending(task.task_id, task.status)

    def resume_if_ready(
        self, context: TrustedContext, action_id: str
    ) -> IntegrationExecution | None:
        try:
            self._controller.get_mcp_task(context, action_id)
        except ResourceNotFound:
            try:
                self._controller.get_mcp_continuation(context, action_id)
            except ResourceNotFound:
                return None
            return self.resume_input(context, action_id)
        return self.update_task(context, action_id)

    def invoke(
        self,
        context: TrustedContext,
        action_id: str,
        bound: BoundOperation,
        *,
        idempotency_key: str,
    ) -> IntegrationExecution:
        del idempotency_key  # admission owns local idempotency; MCP semantics are profile-specific.
        integration_id, tool_name = self._parse_capability(bound.capability.id)
        binding, client = self._integration(integration_id)
        rpc = self._controller.begin_mcp_rpc(
            context,
            action_id,
            integration_id=integration_id,
            profile=binding.profile,
            method="tools/call",
            request_payload={"name": tool_name, "arguments": bound.parameters},
        )
        try:
            result = client.call_tool(tool_name, bound.parameters)
        except MCPProtocolFault as exc:
            self._record_protocol_fault(rpc.id, exc)
            raise self._protocol_error(exc) from exc
        except HarnessError:
            self._controller.complete_mcp_rpc(
                rpc.id,
                status="protocol_error",
                response_summary={"phase": "local_binding_rejected"},
            )
            raise
        except Exception as exc:
            return self._record_unknown_dispatch(action_id, rpc.id, exc)
        return self._apply_invocation(
            context,
            action_id,
            rpc.id,
            binding,
            tool_name,
            result,
        )

    def resume_input(self, context: TrustedContext, action_id: str) -> IntegrationExecution:
        """Resume MRTR on the same Action with persisted opaque requestState."""

        continuation = self._controller.get_mcp_continuation(context, action_id)
        if continuation.status == "resumed":
            raise VersionConflict()
        if continuation.response_values is None:
            raise InvalidOperation("MCP continuation has not received input")
        binding, client = self._integration(continuation.integration_id)
        if binding.profile != continuation.profile:
            raise InvalidOperation("MCP continuation profile changed")
        action = self._start_resumed_action(context, action_id)
        responses = self._input_responses(
            continuation.input_requests,
            continuation.response_values,
        )
        rpc = self._controller.begin_mcp_rpc(
            context,
            action_id,
            integration_id=continuation.integration_id,
            profile=binding.profile,
            method="tools/call",
            request_payload={
                "name": continuation.tool_name,
                "arguments": action.bound_operation,
                "inputResponses": responses,
                "hasRequestState": continuation.request_state is not None,
            },
        )
        self._controller.mark_mcp_continuation_resumed(continuation.id)
        try:
            result = client.call_tool(
                continuation.tool_name,
                action.bound_operation,
                input_responses=responses,
                request_state=continuation.request_state,
            )
        except MCPProtocolFault as exc:
            self._record_protocol_fault(rpc.id, exc)
            self._controller.mark_action_outcome_unknown(
                action_id,
                receipt={"phase": "mcp_continuation_protocol_error", "code": exc.code},
            )
            return IntegrationExecution(
                {"result_type": "protocol_error", "code": exc.code}, 202, False, False
            )
        except HarnessError:
            self._controller.complete_mcp_rpc(
                rpc.id,
                status="protocol_error",
                response_summary={"phase": "local_binding_rejected"},
            )
            raise
        except Exception as exc:
            return self._record_unknown_dispatch(action_id, rpc.id, exc)
        outcome = self._apply_invocation(
            context,
            action_id,
            rpc.id,
            binding,
            continuation.tool_name,
            result,
        )
        if outcome.terminal and not outcome.committed and isinstance(result, MCPComplete):
            self._controller.complete_action(
                action_id,
                result={"status_code": outcome.status_code, "value": outcome.value},
                succeeded=outcome.succeeded,
                transport_kind="mcp",
                response_status=outcome.status_code,
            )
            self._finish_implicit_if_needed(context, action_id, succeeded=outcome.succeeded)
        return outcome

    def poll_task(
        self,
        context: TrustedContext,
        action_id: str,
        *,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> IntegrationExecution:
        task = self._controller.get_mcp_task(context, action_id)
        binding, client = self._integration(task.integration_id)
        if binding.profile != task.profile:
            raise InvalidOperation("MCP task profile changed")
        rpc = self._controller.begin_mcp_rpc(
            context,
            action_id,
            integration_id=task.integration_id,
            profile=binding.profile,
            method="tasks/get",
            request_payload={"taskId": task.task_id},
            claimed_job=claimed_job,
            worker_id=worker_id,
        )
        try:
            result = client.get_task(task.task_id)
        except MCPProtocolFault as exc:
            self._record_protocol_fault(rpc.id, exc)
            raise self._protocol_error(exc) from exc
        except HarnessError:
            self._controller.complete_mcp_rpc(
                rpc.id,
                status="protocol_error",
                response_summary={"phase": "local_binding_rejected"},
            )
            raise
        except Exception as exc:
            self._controller.complete_mcp_rpc(
                rpc.id,
                status="outcome_unknown",
                response_summary={"exception_type": type(exc).__name__},
            )
            raise HarnessError(
                503,
                "mcp_poll_unavailable",
                "MCP task poll is unavailable",
                "The task handle remains durable and can be polled again",
                "after_refresh",
            ) from exc
        prompt: str | None = None
        schema: dict[str, Any] | None = None
        if result.status == "input_required":
            prompt, schema = self._prompt_schema(result.input_requests)
        persisted, input_request = self._controller.record_mcp_task_observation(
            context,
            action_id,
            rpc_id=rpc.id,
            task=self._task_dict(result),
            prompt=prompt,
            requested_schema=schema,
            expires_at=(datetime.now(UTC) + self._input_timeout if prompt is not None else None),
            claimed_job=claimed_job,
            worker_id=worker_id,
        )
        if persisted.status == "working":
            return self._task_pending(persisted.task_id, persisted.status)
        if persisted.status == "input_required":
            if input_request is None:
                raise RuntimeError("task input request was not persisted")
            return IntegrationExecution(
                {
                    "result_type": "input_required",
                    "task_id": persisted.task_id,
                    "input_request_id": input_request.id,
                    "location": f"/v1/input-requests/{input_request.id}",
                },
                202,
                False,
                False,
            )
        if persisted.status == "cancelled":
            self._controller.finalize_mcp_task_cancelled(
                context, action_id, claimed_job=claimed_job, worker_id=worker_id
            )
            self._finish_implicit_if_needed(context, action_id, succeeded=False)
            return IntegrationExecution(
                {"result_type": "task", "task_id": persisted.task_id, "status": "cancelled"},
                200,
                False,
                committed=True,
            )
        return self._complete_task(
            context,
            action_id,
            persisted.task_id,
            result,
            claimed_job=claimed_job,
            worker_id=worker_id,
        )

    def process_poll_job(
        self,
        context: TrustedContext,
        claimed: ClaimedJob,
        *,
        worker_id: str,
    ) -> IntegrationExecution:
        """Scheduler entry point: poll a persisted handle, never repeat tools/call."""

        if claimed.kind != "mcp_task_poll" or claimed.action_id is None:
            raise InvalidOperation("job is not an MCP task poll")
        outcome = self.poll_task(
            context, claimed.action_id, claimed_job=claimed, worker_id=worker_id
        )
        self._controller.complete_job(claimed, worker_id)
        return outcome

    def update_task(self, context: TrustedContext, action_id: str) -> IntegrationExecution:
        task = self._controller.get_mcp_task(context, action_id)
        _, response_values = self._controller.get_latest_external_input_response(context, action_id)
        binding, client = self._integration(task.integration_id)
        self._start_resumed_action(context, action_id)
        responses = self._input_responses(task.input_requests, response_values)
        rpc = self._controller.begin_mcp_rpc(
            context,
            action_id,
            integration_id=task.integration_id,
            profile=binding.profile,
            method="tasks/update",
            request_payload={"taskId": task.task_id, "inputResponses": responses},
        )
        try:
            client.update_task(task.task_id, responses)
        except MCPProtocolFault as exc:
            self._record_protocol_fault(rpc.id, exc)
            self._controller.mark_action_outcome_unknown(
                action_id,
                receipt={"phase": "mcp_task_update_protocol_error", "code": exc.code},
                upstream_handle=task.task_id,
            )
            return IntegrationExecution(
                {"result_type": "protocol_error", "code": exc.code}, 202, False, False
            )
        except HarnessError:
            self._controller.complete_mcp_rpc(
                rpc.id,
                status="protocol_error",
                response_summary={"phase": "local_binding_rejected"},
            )
            raise
        except Exception as exc:
            return self._record_unknown_dispatch(action_id, rpc.id, exc)
        self._controller.mark_mcp_task_update_acknowledged(context, action_id, rpc_id=rpc.id)
        return self._task_pending(task.task_id, "working")

    def cancel_task(self, context: TrustedContext, action_id: str) -> IntegrationExecution:
        task = self._controller.get_mcp_task(context, action_id)
        binding, client = self._integration(task.integration_id)
        rpc = self._controller.begin_mcp_rpc(
            context,
            action_id,
            integration_id=task.integration_id,
            profile=binding.profile,
            method="tasks/cancel",
            request_payload={"taskId": task.task_id},
        )
        try:
            client.cancel_task(task.task_id)
        except MCPProtocolFault as exc:
            self._record_protocol_fault(rpc.id, exc)
            raise self._protocol_error(exc) from exc
        except Exception as exc:
            self._controller.complete_mcp_rpc(
                rpc.id,
                status="outcome_unknown",
                response_summary={"exception_type": type(exc).__name__},
            )
            return IntegrationExecution(
                {
                    "result_type": "task",
                    "task_id": task.task_id,
                    "status": task.status,
                    "cancel_acknowledged": False,
                    "stopped": False,
                },
                202,
                False,
                False,
            )
        persisted = self._controller.mark_mcp_task_cancel_acknowledged(
            context, action_id, rpc_id=rpc.id
        )
        return IntegrationExecution(
            {
                "result_type": "task",
                "task_id": persisted.task_id,
                "status": persisted.status,
                "cancel_acknowledged": True,
                "stopped": False,
            },
            202,
            False,
            False,
        )

    def _apply_invocation(
        self,
        context: TrustedContext,
        action_id: str,
        rpc_id: str,
        binding: MCPIntegrationBinding,
        tool_name: str,
        result: MCPInvocation,
    ) -> IntegrationExecution:
        if isinstance(result, MCPComplete):
            self._controller.complete_mcp_rpc(
                rpc_id,
                status="complete",
                response_summary={
                    "result_type": "complete",
                    "is_error": result.is_error,
                    "content_count": len(result.content),
                },
            )
            return IntegrationExecution(
                {
                    "result_type": "complete",
                    "content": list(result.content),
                    "structured_content": result.structured_content,
                    "is_error": result.is_error,
                },
                200,
                not result.is_error,
            )
        if isinstance(result, MCPInputRequired):
            if binding.profile != "modern2026-07-28":
                raise InvalidOperation("legacy MCP profile returned input_required")
            prompt, schema = self._prompt_schema(result.input_requests)
            request = self._controller.record_mcp_input_required(
                context,
                action_id,
                rpc_id=rpc_id,
                integration_id=binding.integration_id,
                profile=binding.profile,
                tool_name=tool_name,
                request_state=result.request_state,
                input_requests=result.input_requests,
                prompt=prompt,
                requested_schema=schema,
                expires_at=datetime.now(UTC) + self._input_timeout,
            )
            return IntegrationExecution(
                {
                    "result_type": "input_required",
                    "input_request_id": request.id,
                    "location": f"/v1/input-requests/{request.id}",
                },
                202,
                False,
                False,
            )
        if not binding.tasks_enabled:
            raise InvalidOperation("MCP server returned an unadvertised task result")
        persisted = self._controller.record_mcp_task(
            context,
            action_id,
            rpc_id=rpc_id,
            integration_id=binding.integration_id,
            profile=binding.profile,
            task=self._task_dict(result),
        )
        if persisted.status in {"completed", "failed"}:
            return self._complete_task(context, action_id, persisted.task_id, result)
        if persisted.status == "cancelled":
            self._controller.finalize_mcp_task_cancelled(context, action_id)
            self._finish_implicit_if_needed(context, action_id, succeeded=False)
            return IntegrationExecution(
                {"result_type": "task", "task_id": persisted.task_id, "status": "cancelled"},
                200,
                False,
                committed=True,
            )
        return self._task_pending(persisted.task_id, persisted.status)

    def _complete_task(
        self,
        context: TrustedContext,
        action_id: str,
        task_id: str,
        task: MCPTask,
        *,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> IntegrationExecution:
        if not self._controller.begin_mcp_task_completion(
            action_id, claimed_job=claimed_job, worker_id=worker_id
        ):
            raise VersionConflict()
        if task.status == "completed":
            value = {
                "result_type": "task",
                "task_id": task_id,
                "status": task.status,
                "result": task.result,
            }
            succeeded = not bool((task.result or {}).get("isError", False))
            status_code = 200
        else:
            value = {
                "result_type": "task",
                "task_id": task_id,
                "status": task.status,
                "error": task.error,
            }
            succeeded = False
            status_code = 502
        self._controller.complete_action(
            action_id,
            result={"status_code": status_code, "value": value},
            succeeded=succeeded,
            transport_kind="mcp",
            response_status=status_code,
            claimed_job=claimed_job,
            worker_id=worker_id,
        )
        self._finish_implicit_if_needed(context, action_id, succeeded=succeeded)
        return IntegrationExecution(value, status_code, succeeded, committed=True)

    def _finish_implicit_if_needed(
        self,
        context: TrustedContext,
        action_id: str,
        *,
        succeeded: bool,
    ) -> None:
        action = self._controller.get_action(context, action_id)
        run = self._controller.get_run(context, action.run_id)
        if run.agent_id == "system.http-resource" and run.status == RunStatus.RUNNING:
            self._controller.finish_implicit_run(
                run.id, succeeded=succeeded, actor_ref="mcp_adapter"
            )

    def _start_resumed_action(self, context: TrustedContext, action_id: str) -> Any:
        action = self._controller.get_action(context, action_id)
        self._controller.ensure_run_running(action.run_id, actor_ref="mcp_adapter")
        if action.status != ActionStatus.READY or not self._controller.begin_action_dispatch(
            action_id
        ):
            raise VersionConflict()
        return self._controller.get_action(context, action_id)

    def _integration(self, integration_id: str) -> tuple[MCPIntegrationBinding, MCPClient]:
        try:
            return self._integrations[integration_id]
        except KeyError as exc:
            raise InvalidOperation("MCP integration is not configured") from exc

    @staticmethod
    def _parse_capability(capability_id: str) -> tuple[str, str]:
        if not capability_id.startswith("mcp."):
            raise InvalidOperation("capability is not MCP-backed")
        parts = capability_id.split(".", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            raise InvalidOperation("invalid MCP capability identifier")
        return parts[1], parts[2]

    @staticmethod
    def _input_responses(
        input_requests: dict[str, Any],
        values: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        responses: dict[str, dict[str, Any]] = {}
        for key in input_requests:
            content = values.get(key) if len(input_requests) > 1 else values
            if not isinstance(content, dict):
                raise InvalidOperation("MCP form response must be a JSON object")
            responses[key] = {"action": "accept", "content": content}
        return responses

    @staticmethod
    def _prompt_schema(input_requests: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        if not input_requests:
            raise InvalidOperation("MCP input_required result has no requests")
        prompts: list[str] = []
        schemas: dict[str, Any] = {}
        for key, request in input_requests.items():
            if request.get("method") != "elicitation/create":
                raise InvalidOperation("only MCP form elicitation is supported")
            params = request.get("params")
            if not isinstance(params, dict) or params.get("mode", "form") != "form":
                raise InvalidOperation("only MCP form elicitation is supported")
            requested = params.get("requestedSchema")
            if not isinstance(requested, dict):
                raise InvalidOperation("MCP elicitation schema is missing")
            prompts.append(str(params.get("message") or key))
            schemas[key] = requested
        if len(schemas) == 1:
            key = next(iter(schemas))
            return prompts[0], schemas[key]
        return (
            "\n".join(f"{key}: {prompt}" for key, prompt in zip(schemas, prompts, strict=True)),
            {
                "type": "object",
                "properties": schemas,
                "required": list(schemas),
                "additionalProperties": False,
            },
        )

    @staticmethod
    def _task_dict(task: MCPTask) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "status": task.status,
            "created_at": task.created_at,
            "last_updated_at": task.last_updated_at,
            "ttl_ms": task.ttl_ms,
            "poll_interval_ms": task.poll_interval_ms,
            "status_message": task.status_message,
            "input_requests": task.input_requests,
            "result": task.result,
            "error": task.error,
        }

    @staticmethod
    def _task_pending(task_id: str, status: str) -> IntegrationExecution:
        return IntegrationExecution(
            {"result_type": "task", "task_id": task_id, "status": status},
            202,
            False,
            False,
        )

    def _record_protocol_fault(self, rpc_id: str, fault: MCPProtocolFault) -> None:
        self._controller.complete_mcp_rpc(
            rpc_id,
            status="protocol_error",
            response_summary={"code": fault.code, "message": fault.message, "data": fault.data},
        )

    def _record_unknown_dispatch(
        self, action_id: str, rpc_id: str, error: Exception
    ) -> IntegrationExecution:
        self._controller.complete_mcp_rpc(
            rpc_id,
            status="outcome_unknown",
            response_summary={"exception_type": type(error).__name__},
        )
        self._controller.mark_action_outcome_unknown(
            action_id,
            receipt={
                "phase": "mcp_response_unavailable",
                "possible_dispatch": True,
                "exception_type": type(error).__name__,
            },
        )
        return IntegrationExecution(
            {"result_type": "outcome_unknown", "action_id": action_id},
            202,
            False,
            False,
        )

    @staticmethod
    def _protocol_error(fault: MCPProtocolFault) -> HarnessError:
        return HarnessError(
            502,
            "mcp_protocol_error",
            "MCP protocol request failed",
            f"{fault.code}: {fault.message}",
            "after_refresh",
        )
