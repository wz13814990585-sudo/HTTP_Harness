"""Real PostgreSQL evaluation plumbing with a mocked model, not live evidence."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from time import monotonic, sleep
from typing import Annotated, Any, TypedDict

import httpx
import httpx2
import pytest
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from pydantic import Field
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from hnh.adapters.mcp.sdk import OfficialSDKMCPClient
from hnh.adapters.models.openai_responses import (
    OpenAIFunctionToolsProvider,
    OpenAIResponsesProvider,
)
from hnh.adapters.models.scripted import ScriptedProvider
from hnh.adapters.postgres.models import MCPRPCRecord, RunRecord
from hnh.application.mcp_catalog import MCPIntegrationBinding
from hnh.application.mcp_composition import MCPIntegrationManager
from hnh.application.run_controller import RunController, request_fingerprint
from hnh.domain.errors import ProviderUnavailable
from hnh.domain.identity import TrustedContext
from hnh.domain.states import RunStatus
from hnh.evaluation import (
    EvaluationCase,
    EvaluationControls,
    EvaluationDesignError,
    ExperimentRunner,
    stable_hash,
    summarize,
)
from hnh.evaluation_live import LiveEchoExecutor, live_echo_cases
from hnh.ports.models import CompleteModelResponse, ContextSnapshot, ModelLimits


class EchoOutput(TypedDict):
    message: str


class NarrowEchoOutput(TypedDict):
    message: Annotated[str, Field(max_length=99)]


def _mock_echo_provider(
    cases: tuple[EvaluationCase, ...],
    surface: str,
    first_requests: list[dict[str, Any]] | None = None,
) -> OpenAIResponsesProvider:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        if calls == 1 and first_requests is not None:
            first_requests.append(body)
        task_context = json.loads(body["input"])["context"]
        expected = next(
            case.expected_evidence[0].removeprefix("echo:")
            for case in cases
            if case.task == task_context["goal"]
        )
        if calls == 1:
            if surface == "native_function_schema":
                output = [
                    {
                        "type": "function_call",
                        "call_id": "call-echo",
                        "name": body["tools"][0]["name"],
                        "arguments": json.dumps({"message": expected}),
                    }
                ]
            else:
                capability = json.loads(body["input"])["capabilities"][0]
                turn = {
                    "public_output": "",
                    "operations": [
                        {
                            "method": "POST",
                            "target": capability["path_template"],
                            "query": [],
                            "headers": {"content-type": "application/json"},
                            "payload": {"kind": "json", "value": {"message": expected}},
                        }
                    ],
                    "final_candidate": None,
                    "request_input": None,
                }
                output = [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(turn)}],
                    }
                ]
        else:
            action_id = task_context["observations"][0]["action_id"]
            turn = {
                "public_output": "Echo observed.",
                "operations": [] if surface == "http_semantic_operation" else None,
                "final_candidate": {
                    "answer": "Echo observed.",
                    "artifact_ids": [],
                    "evidence_ids": [action_id],
                    "acceptance_claims": [f"echo returned {expected}"],
                },
                "request_input": None,
            }
            if surface == "native_function_schema":
                del turn["operations"]
            output = [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(turn)}],
                }
            ]
        return httpx.Response(
            200,
            json={
                "id": f"mock-{calls}",
                "status": "completed",
                "output": output,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    adapter = (
        OpenAIFunctionToolsProvider
        if surface == "native_function_schema"
        else OpenAIResponsesProvider
    )
    return adapter(
        api_key="mock-only",
        model="controlled-model",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )


@pytest.mark.postgres
def test_four_case_four_arm_entrypoint_records_committed_echo_and_usage(
    clean_postgres: Engine, tmp_path: Path
) -> None:
    server: MCPServer[Any] = MCPServer("p08-live-entrypoint-local")

    @server.tool(structured_output=True)
    def echo(message: str) -> EchoOutput:
        """Echo the exact message."""
        return {"message": message}

    binding = MCPIntegrationBinding("eval", "modern2026-07-28", tool_effects={"echo": "read_only"})
    manager = MCPIntegrationManager(
        (binding,),
        lambda configured, context: OfficialSDKMCPClient(
            server,
            configured,
            subject_id=context.subject_id,
            tenant_id=context.tenant_id,
        ),
    )
    context = TrustedContext(
        "tenant-p08-live-entry",
        "subject-p08-live-entry",
        frozenset({"runs:read", "integrations:invoke"}),
    )
    cases = live_echo_cases()

    controls = EvaluationControls(
        model="controlled-model",
        model_settings={"stream": False},
        capability_revision="controlled-echo",
        policy_revision="dev-1",
        scopes=tuple(sorted(context.scopes)),
        budget={"max_model_turns": 3, "max_tool_calls": 1, "max_output_tokens": 2048},
        timeout_seconds=30,
        fixtures_hash=stable_hash([case.task for case in cases]),
    )
    output = tmp_path / "raw.jsonl"
    executor = LiveEchoExecutor(
        clean_postgres,
        context,
        lambda surface: _mock_echo_provider(cases, surface),
        manager,
        campaign_id="controlled-entrypoint",
    )
    controls = replace(controls, capability_revision=executor.preflight())
    rows = ExperimentRunner(executor, output).run(cases, controls)
    assert len(rows) == 16
    assert {row["evidence_tier"] for row in rows} == {"controlled"}
    assert all(row["verified_completion"] is True for row in rows)
    assert all(row["model_input_tokens"] == 20 for row in rows)
    assert all(row["model_output_tokens"] == 10 for row in rows)
    assert all(row["run_id"] for row in rows)
    assert all(len(row["model_call_ids"]) == 2 for row in rows)
    assert all(len(row["action_ids"]) == 1 for row in rows)
    assert all(row["event_seq_start"] == 1 for row in rows)
    assert all(row["event_seq_end"] > 0 for row in rows)
    assert len(output.read_text().splitlines()) == 16
    assert summarize(rows)["design_complete"] is True
    assert summarize(rows)["evidence_tier"] == "controlled"
    assert summarize(rows)["complete_case_count"] == 4
    assert {row["case_id"] for row in rows} == {case.case_id for case in cases}
    assert {row["capability_revision"] for row in rows} == {controls.capability_revision}


@pytest.mark.postgres
def test_echo_preflight_rejects_incomparable_mcp_schema_before_model_or_run(
    clean_postgres: Engine,
) -> None:
    server: MCPServer[Any] = MCPServer("p08-incomparable-echo")

    @server.tool(structured_output=True)
    def echo(value: int) -> dict[str, int]:
        """This is not the frozen string-message contract."""
        return {"value": value}

    context = TrustedContext(
        "tenant-p08-incomparable",
        "subject-p08-incomparable",
        frozenset({"runs:read", "integrations:invoke"}),
    )
    binding = MCPIntegrationBinding("eval", "modern2026-07-28", tool_effects={"echo": "read_only"})
    manager = MCPIntegrationManager(
        (binding,),
        lambda configured, actor: OfficialSDKMCPClient(
            server,
            configured,
            subject_id=actor.subject_id,
            tenant_id=actor.tenant_id,
        ),
    )
    executor = LiveEchoExecutor(
        clean_postgres,
        context,
        lambda _surface: pytest.fail("model must not be called before comparability preflight"),
        manager,
        campaign_id="incomparable-echo",
    )
    with pytest.raises(EvaluationDesignError, match="required string message"):
        executor.preflight()
    with Session(clean_postgres) as session:
        assert session.scalar(select(func.count()).select_from(RunRecord)) == 0


@pytest.mark.postgres
def test_echo_preflight_rejects_remote_schema_narrower_than_canonical_domain(
    clean_postgres: Engine,
) -> None:
    server: MCPServer[Any] = MCPServer("p08-narrow-echo")

    @server.tool(structured_output=True)
    def echo(message: Annotated[str, Field(max_length=99)]) -> dict[str, str]:
        """All frozen messages fit, but the canonical 100-character input does not."""
        return {"message": message}

    context = TrustedContext(
        "tenant-p08-narrow",
        "subject-p08-narrow",
        frozenset({"runs:read", "integrations:invoke"}),
    )
    binding = MCPIntegrationBinding("eval", "modern2026-07-28", tool_effects={"echo": "read_only"})
    manager = MCPIntegrationManager(
        (binding,),
        lambda configured, actor: OfficialSDKMCPClient(
            server,
            configured,
            subject_id=actor.subject_id,
            tenant_id=actor.tenant_id,
        ),
    )
    executor = LiveEchoExecutor(
        clean_postgres,
        context,
        lambda _surface: pytest.fail("model must not be called before comparability preflight"),
        manager,
        campaign_id="narrow-echo",
    )
    with pytest.raises(EvaluationDesignError, match="narrower than the canonical"):
        executor.preflight()
    with Session(clean_postgres) as session:
        assert session.scalar(select(func.count()).select_from(RunRecord)) == 0


@pytest.mark.postgres
def test_echo_preflight_rejects_unproven_remote_output_before_model_or_run(
    clean_postgres: Engine,
) -> None:
    server: MCPServer[Any] = MCPServer("p08-unproven-echo-output")

    @server.tool(structured_output=True)
    def echo(message: str) -> dict[str, str]:
        """A generic dictionary does not promise the required message key."""
        return {"message": message}

    context = TrustedContext(
        "tenant-p08-unproven-output",
        "subject-p08-unproven-output",
        frozenset({"runs:read", "integrations:invoke"}),
    )
    binding = MCPIntegrationBinding("eval", "modern2026-07-28", tool_effects={"echo": "read_only"})
    manager = MCPIntegrationManager(
        (binding,),
        lambda configured, actor: OfficialSDKMCPClient(
            server,
            configured,
            subject_id=actor.subject_id,
            tenant_id=actor.tenant_id,
        ),
    )
    executor = LiveEchoExecutor(
        clean_postgres,
        context,
        lambda _surface: pytest.fail("model must not be called before comparability preflight"),
        manager,
        campaign_id="unproven-echo-output",
    )
    with pytest.raises(EvaluationDesignError, match="required string message output"):
        executor.preflight()
    with Session(clean_postgres) as session:
        assert session.scalar(select(func.count()).select_from(RunRecord)) == 0


@pytest.mark.postgres
def test_echo_preflight_rejects_remote_output_narrower_than_canonical_domain(
    clean_postgres: Engine,
) -> None:
    server: MCPServer[Any] = MCPServer("p08-narrow-echo-output")

    @server.tool(structured_output=True)
    def echo(message: str) -> NarrowEchoOutput:
        """The declared result excludes valid 100-character echo messages."""
        return {"message": message}

    context = TrustedContext(
        "tenant-p08-narrow-output",
        "subject-p08-narrow-output",
        frozenset({"runs:read", "integrations:invoke"}),
    )
    binding = MCPIntegrationBinding("eval", "modern2026-07-28", tool_effects={"echo": "read_only"})
    manager = MCPIntegrationManager(
        (binding,),
        lambda configured, actor: OfficialSDKMCPClient(
            server,
            configured,
            subject_id=actor.subject_id,
            tenant_id=actor.tenant_id,
        ),
    )
    executor = LiveEchoExecutor(
        clean_postgres,
        context,
        lambda _surface: pytest.fail("model must not be called before comparability preflight"),
        manager,
        campaign_id="narrow-echo-output",
    )
    with pytest.raises(EvaluationDesignError, match="output is narrower than the canonical"):
        executor.preflight()
    with Session(clean_postgres) as session:
        assert session.scalar(select(func.count()).select_from(RunRecord)) == 0


@pytest.mark.postgres
def test_echo_runner_rejects_unpinned_control_revision_before_model_or_run(
    clean_postgres: Engine,
) -> None:
    server: MCPServer[Any] = MCPServer("p08-control-revision-drift")

    @server.tool(structured_output=True)
    def echo(message: str) -> EchoOutput:
        """Echo the exact message."""
        return {"message": message}

    context = TrustedContext(
        "tenant-p08-control-drift",
        "subject-p08-control-drift",
        frozenset({"runs:read", "integrations:invoke"}),
    )
    binding = MCPIntegrationBinding("eval", "modern2026-07-28", tool_effects={"echo": "read_only"})
    manager = MCPIntegrationManager(
        (binding,),
        lambda configured, actor: OfficialSDKMCPClient(
            server,
            configured,
            subject_id=actor.subject_id,
            tenant_id=actor.tenant_id,
        ),
    )
    executor = LiveEchoExecutor(
        clean_postgres,
        context,
        lambda _surface: pytest.fail("model must not be called with unpinned controls"),
        manager,
        campaign_id="control-revision-drift",
    )
    actual_revision = executor.preflight()
    controls = EvaluationControls(
        model="controlled-model",
        model_settings={"stream": False},
        capability_revision=actual_revision + "-wrong",
        policy_revision="dev-1",
        scopes=tuple(sorted(context.scopes)),
        budget={"max_model_turns": 3, "max_tool_calls": 1, "max_output_tokens": 2048},
        timeout_seconds=30,
        fixtures_hash=stable_hash([live_echo_cases()[0].task]),
    )
    with pytest.raises(EvaluationDesignError, match="preflight capability revision"):
        executor.run(live_echo_cases()[0], controls, "http_semantic_operation", "native_http")
    with Session(clean_postgres) as session:
        assert session.scalar(select(func.count()).select_from(RunRecord)) == 0


@pytest.mark.postgres
def test_four_arm_echo_uses_separate_mcp_process_over_real_http(
    clean_postgres: Engine, tmp_path: Path
) -> None:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = int(reservation.getsockname()[1])
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests.support.mcp_process_server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env={name: value for name, value in os.environ.items() if not name.startswith("HNH_")},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    endpoint = f"http://127.0.0.1:{port}/mcp"

    @asynccontextmanager
    async def transport() -> AsyncIterator[Any]:
        async with httpx2.AsyncClient(follow_redirects=False, trust_env=False) as http_client:
            async with streamable_http_client(endpoint, http_client=http_client) as streams:
                yield streams

    class ReconnectableTransport:
        """The SDK client discovers and invokes in separate connection contexts."""

        async def __aenter__(self) -> Any:
            self._active = transport()
            return await self._active.__aenter__()

        async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
            return await self._active.__aexit__(exc_type, exc_value, traceback)

    try:
        deadline = monotonic() + 10
        while True:
            if process.poll() is not None:
                raise AssertionError("MCP process exited before binding its loopback port")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                if monotonic() >= deadline:
                    raise AssertionError("MCP process did not start on loopback") from None
                sleep(0.1)

        context = TrustedContext(
            "tenant-p08-process-echo",
            "subject-p08-process-echo",
            frozenset({"runs:read", "integrations:invoke"}),
        )
        cases = (live_echo_cases()[0],)
        binding = MCPIntegrationBinding(
            "eval", "modern2026-07-28", tool_effects={"echo": "read_only"}
        )
        manager = MCPIntegrationManager(
            (binding,),
            lambda configured, actor: OfficialSDKMCPClient(
                ReconnectableTransport(),
                configured,
                subject_id=actor.subject_id,
                tenant_id=actor.tenant_id,
            ),
        )
        controls = EvaluationControls(
            model="controlled-model",
            model_settings={"stream": False},
            capability_revision="controlled-echo-process",
            policy_revision="dev-1",
            scopes=tuple(sorted(context.scopes)),
            budget={"max_model_turns": 3, "max_tool_calls": 1, "max_output_tokens": 2048},
            timeout_seconds=30.0,
            fixtures_hash=stable_hash([case.task for case in cases]),
        )
        first_requests: list[dict[str, Any]] = []
        executor = LiveEchoExecutor(
            clean_postgres,
            context,
            lambda surface: _mock_echo_provider(cases, surface, first_requests),
            manager,
            campaign_id="process-echo",
        )
        controls = replace(controls, capability_revision=executor.preflight())
        rows = ExperimentRunner(executor, tmp_path / "process-echo.jsonl").run(cases, controls)
        assert len(rows) == 4
        assert len(first_requests) == 4
        function_native, function_mcp, http_native, http_mcp = first_requests
        assert function_native["tools"] == function_mcp["tools"]
        native_catalog = json.loads(http_native["input"])["capabilities"][0]
        mcp_catalog = json.loads(http_mcp["input"])["capabilities"][0]
        assert native_catalog == mcp_catalog
        assert native_catalog["id"] == "native.echo"
        assert native_catalog["path_template"] == (
            "/v1/integrations/native/operations/echo/invocations"
        )
        controller = RunController(clean_postgres)
        assert all(row["verified_completion"] is True for row in rows)
        assert summarize(rows)["design_complete"] is True
        visible_results = []
        for row in rows:
            run_id = row["run_id"]
            assert isinstance(run_id, str)
            snapshot = controller.get_context_snapshot(context, run_id, 2)
            observation = snapshot.content["observations"][0]
            visible_results.append(observation["result"])
        assert visible_results == [{"status_code": 200, "value": {"message": "same-input"}}] * 4
        remote_rows = [row for row in rows if row["downstream"] == "mcp_adapter"]
        assert len(remote_rows) == 2
        for row in remote_rows:
            run_id = row["run_id"]
            assert isinstance(run_id, str)
            actions = controller.list_actions(context, run_id)
            assert len(actions) == 1
            assert actions[0].capability_id == "native.echo"
            assert actions[0].result is not None
            assert actions[0].result["value"]["structured_content"]["server_pid"] == process.pid
            with Session(clean_postgres) as session:
                rpc = session.scalar(
                    select(MCPRPCRecord).where(MCPRPCRecord.action_id == actions[0].id)
                )
                assert rpc is not None
                assert rpc.integration_id == "eval"
                assert rpc.method == "tools/call"
                assert rpc.request_hash == request_fingerprint(
                    "tools/call",
                    "eval",
                    {"name": "echo", "arguments": {"message": "same-input"}},
                )
        assert process.pid != os.getpid()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.postgres
def test_live_echo_provider_timeout_is_not_reported_as_generic_failure(
    clean_postgres: Engine,
) -> None:
    class TimeoutProvider(ScriptedProvider):
        def generate(
            self,
            context: ContextSnapshot,
            tools: list[dict[str, Any]],
            limits: ModelLimits,
        ) -> CompleteModelResponse:
            del context, tools, limits
            raise ProviderUnavailable("provider timed out") from httpx.ReadTimeout("test timeout")

    context = TrustedContext(
        "tenant-p08-echo-timeout",
        "subject-p08-echo-timeout",
        frozenset({"runs:read", "integrations:invoke"}),
    )
    controls = EvaluationControls(
        "controlled-model",
        {},
        "native.echo:1",
        "dev-1",
        tuple(sorted(context.scopes)),
        {"max_model_turns": 3, "max_tool_calls": 1, "max_output_tokens": 2048},
        30.0,
        stable_hash("echo-timeout-fixture"),
    )
    executor = LiveEchoExecutor(
        clean_postgres,
        context,
        lambda _surface: TimeoutProvider([], model_revision="controlled-model"),
        MCPIntegrationManager((), lambda _binding, _context: pytest.fail("MCP not called")),
        campaign_id="echo-timeout",
    )
    result = executor.run(live_echo_cases()[0], controls, "http_semantic_operation", "native_http")
    assert result.outcome == "timeout"
    assert result.failure_reason == "provider timeout"
    assert result.run_id is not None
    assert result.model_call_ids
    run = RunController(clean_postgres).get_run(context, result.run_id)
    assert run.status == RunStatus.FAILED
    assert run.failure == {"code": "evaluation_stopped", "detail": "provider timeout"}


@pytest.mark.postgres
def test_echo_call_returning_after_deadline_is_recorded_as_timeout(
    clean_postgres: Engine,
) -> None:
    clock = [100.0]

    class SlowProvider(ScriptedProvider):
        def generate(
            self,
            context: ContextSnapshot,
            tools: list[dict[str, Any]],
            limits: ModelLimits,
        ) -> CompleteModelResponse:
            clock[0] += 1.0
            return super().generate(context, tools, limits)

    context = TrustedContext(
        "tenant-p08-echo-late",
        "subject-p08-echo-late",
        frozenset({"runs:read", "integrations:invoke"}),
    )
    provider = SlowProvider([{"operations": []}], model_revision="controlled-model")
    executor = LiveEchoExecutor(
        clean_postgres,
        context,
        lambda _surface: provider,
        campaign_id="echo-late-return",
        monotonic_clock=lambda: clock[0],
    )
    controls = EvaluationControls(
        "controlled-model",
        {},
        "native.echo:1",
        "dev-1",
        tuple(sorted(context.scopes)),
        {"max_model_turns": 3, "max_tool_calls": 1, "max_output_tokens": 2048},
        0.25,
        stable_hash("echo-late-fixture"),
    )
    result = executor.run(live_echo_cases()[0], controls, "http_semantic_operation", "native_http")
    assert provider.calls == 1
    assert result.outcome == "timeout"
    assert result.failure_reason == "evaluation deadline exceeded"
    assert result.model_call_ids
    assert result.run_id is not None
    run = RunController(clean_postgres).get_run(context, result.run_id)
    assert run.status == RunStatus.FAILED
    assert run.failure == {"code": "evaluation_stopped", "detail": "evaluation deadline exceeded"}


@pytest.mark.postgres
def test_echo_completed_after_deadline_is_not_counted_as_timely_success(
    clean_postgres: Engine,
) -> None:
    clock = [100.0]
    message = "same-input"

    class LateSuccessProvider(ScriptedProvider):
        def generate(
            self,
            context: ContextSnapshot,
            tools: list[dict[str, Any]],
            limits: ModelLimits,
        ) -> CompleteModelResponse:
            if self.calls == 0:
                return super().generate(context, tools, limits)
            clock[0] += 1.0
            self.calls += 1
            action_id = context.content["observations"][0]["action_id"]
            final = {
                "final_candidate": {
                    "answer": "Echo observed.",
                    "artifact_ids": [],
                    "evidence_ids": [action_id],
                    "acceptance_claims": ["echo returned the expected message"],
                }
            }
            return CompleteModelResponse(
                provider_request_id="scripted-late-final",
                raw_response={"output": final},
                output=final,
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )

    context = TrustedContext(
        "tenant-p08-echo-late-success",
        "subject-p08-echo-late-success",
        frozenset({"runs:read", "integrations:invoke"}),
    )
    provider = LateSuccessProvider(
        [
            {
                "operations": [
                    {
                        "method": "POST",
                        "target": "/v1/integrations/native/operations/echo/invocations",
                        "headers": {"content-type": "application/json"},
                        "payload": {"kind": "json", "value": {"message": message}},
                    }
                ]
            }
        ],
        model_revision="controlled-model",
    )
    executor = LiveEchoExecutor(
        clean_postgres,
        context,
        lambda _surface: provider,
        campaign_id="echo-late-success",
        monotonic_clock=lambda: clock[0],
    )
    controls = EvaluationControls(
        "controlled-model",
        {},
        "native.echo:1",
        "dev-1",
        tuple(sorted(context.scopes)),
        {"max_model_turns": 3, "max_tool_calls": 1, "max_output_tokens": 2048},
        0.25,
        stable_hash("echo-late-success-fixture"),
    )
    result = executor.run(live_echo_cases()[0], controls, "http_semantic_operation", "native_http")
    assert provider.calls == 2
    assert result.outcome == "timeout"
    assert result.claimed_complete is True
    assert result.accepted_evidence == (f"echo:{message}",)
    assert result.failure_reason == "evaluation deadline exceeded"
    assert result.run_id is not None
    assert (
        RunController(clean_postgres).get_run(context, result.run_id).status == RunStatus.SUCCEEDED
    )
