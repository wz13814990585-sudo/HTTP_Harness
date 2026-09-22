"""Task-suite plumbing over PostgreSQL/Blob with mocked Responses, not live evidence."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from hnh.adapters.models.openai_responses import OpenAIResponsesProvider
from hnh.adapters.models.scripted import ScriptedProvider
from hnh.adapters.postgres.models import ActionRecord, RunRecord
from hnh.application.run_controller import RunController
from hnh.domain.errors import ProviderUnavailable
from hnh.domain.identity import TrustedContext
from hnh.domain.states import RunStatus
from hnh.evaluation import (
    EvaluationControls,
    EvaluationDesignError,
    TaskSuiteRunner,
    stable_hash,
    summarize,
)
from hnh.evaluation_tasks import LiveTaskExecutor, live_task_fixtures
from hnh.ports.models import CompleteModelResponse, ContextSnapshot, ModelLimits, ModelProvider


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("override", "error"),
    (
        ({"capability_revision": "wrong"}, "capability revision"),
        ({"fixtures_hash": "wrong"}, "fixtures"),
        ({"scopes": ("runs:read",)}, "scopes"),
        ({"policy_revision": "wrong"}, "policy revision"),
    ),
)
def test_task_suite_rejects_mislabeled_controls_before_seed_or_run(
    clean_postgres: Engine,
    override: dict[str, object],
    error: str,
) -> None:
    fixtures = live_task_fixtures()
    context = TrustedContext(
        "tenant-p08-task-controls",
        "subject-p08-task-controls",
        frozenset({"runs:read", "workspace:read", "artifacts:write", "artifacts:read"}),
    )
    controls = EvaluationControls(
        "controlled-model",
        {"stream": False},
        "workspace.file.read:1+artifact.create:1",
        "dev-1",
        tuple(sorted(context.scopes)),
        {"max_model_turns": 8, "max_tool_calls": 8, "max_output_tokens": 8192},
        30.0,
        stable_hash([asdict(item) for item in fixtures]),
    )
    executor = LiveTaskExecutor(
        clean_postgres,
        context,
        lambda: pytest.fail("model must not be called with mismatched controls"),
        fixtures,
        campaign_id="bad-task-controls",
    )
    bad_controls = replace(controls, **override)
    with pytest.raises(EvaluationDesignError, match=error):
        executor.preflight(bad_controls)
    with pytest.raises(EvaluationDesignError, match=error):
        executor.run(executor.cases()[0], bad_controls)
    with Session(clean_postgres) as session:
        assert session.scalar(select(func.count()).select_from(RunRecord)) == 0
        assert session.scalar(select(func.count()).select_from(ActionRecord)) == 0


@pytest.mark.postgres
def test_multi_capability_task_suite_records_three_exact_artifact_oracles(
    clean_postgres: Engine, tmp_path: Path
) -> None:
    fixtures = live_task_fixtures()
    context = TrustedContext(
        "tenant-p08-task-suite",
        "subject-p08-task-suite",
        frozenset({"runs:read", "workspace:read", "artifacts:write", "artifacts:read"}),
    )
    controls = EvaluationControls(
        "controlled-model",
        {"stream": False},
        "workspace.file.read:1+artifact.create:1",
        "dev-1",
        tuple(sorted(context.scopes)),
        {"max_model_turns": 8, "max_tool_calls": 8, "max_output_tokens": 8192},
        30.0,
        stable_hash([asdict(item) for item in fixtures]),
    )
    requests: list[dict[str, Any]] = []

    def provider() -> OpenAIResponsesProvider:
        def respond(request: httpx.Request) -> httpx.Response:
            body: dict[str, Any] = json.loads(request.content)
            requests.append(body)
            task_context = json.loads(body["input"])["context"]
            fixture = next(
                item
                for item in fixtures
                if item.evaluation_case("ws-p08-controlled-task").task == task_context["goal"]
            )
            observations = task_context["observations"]
            if not observations:
                turn = {
                    "public_output": "",
                    "operations": [
                        {
                            "method": "GET",
                            "target": (
                                f"/v1/workspaces/ws-p08-controlled-task/files/{fixture.file_name}"
                            ),
                            "query": [],
                            "headers": {},
                            "payload": None,
                        }
                    ],
                    "final_candidate": None,
                    "request_input": None,
                }
            elif len(observations) == 1:
                turn = {
                    "public_output": "",
                    "operations": [
                        {
                            "method": "POST",
                            "target": "/v1/artifacts",
                            "query": [],
                            "headers": {"content-type": "application/octet-stream"},
                            "payload": {
                                "kind": "json",
                                "value": {
                                    "content_base64": base64.b64encode(
                                        fixture.expected_text.encode()
                                    ).decode(),
                                    "media_type": "text/plain",
                                },
                            },
                        }
                    ],
                    "final_candidate": None,
                    "request_input": None,
                }
            else:
                artifact_id = observations[1]["result"]["value"]["id"]
                turn = {
                    "public_output": "Done.",
                    "operations": [],
                    "final_candidate": {
                        "answer": "Done.",
                        "artifact_ids": [artifact_id],
                        "evidence_ids": [item["action_id"] for item in observations],
                        "acceptance_claims": ["exact transformed bytes stored"],
                    },
                    "request_input": None,
                }
            return httpx.Response(
                200,
                json={
                    "id": f"mock-task-{len(requests)}",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": json.dumps(turn)}],
                        }
                    ],
                    "usage": {"input_tokens": 11, "output_tokens": 7},
                },
            )

        return OpenAIResponsesProvider(
            api_key="mock-only",
            model="controlled-model",
            client=httpx.Client(transport=httpx.MockTransport(respond)),
        )

    executor = LiveTaskExecutor(
        clean_postgres,
        context,
        provider,
        fixtures,
        campaign_id="controlled-task",
        blob_root=tmp_path / "blob-store",
    )
    executor.preflight(controls)
    with pytest.raises(ValueError, match="workspace:write"):
        executor.seed(context)
    with Session(clean_postgres) as session:
        assert session.scalar(select(func.count()).select_from(ActionRecord)) == 0
    executor.seed(
        TrustedContext(
            context.tenant_id,
            context.subject_id,
            context.scopes | frozenset({"workspace:write"}),
        )
    )
    raw = tmp_path / "task-suite.raw.jsonl"
    records = TaskSuiteRunner(executor, raw).run(executor.cases(), controls)
    assert len(records) == 3
    assert all(row["verified_completion"] is True for row in records)
    assert all(row["model_input_tokens"] == 33 for row in records)
    assert all(row["model_output_tokens"] == 21 for row in records)
    assert all(len(row["model_call_ids"]) == 3 for row in records)
    assert all(len(row["action_ids"]) == 2 for row in records)
    assert all(len(row["artifact_ids"]) == 1 for row in records)
    assert all(row["event_seq_start"] == 1 for row in records)
    assert all(row["event_seq_end"] > 0 for row in records)
    assert all(row["capability_revision"] == controls.capability_revision for row in records)
    assert len(requests) == 9
    assert all(body["model"] == "controlled-model" for body in requests)
    assert summarize(records)["design_complete"] is True
    assert summarize(records)["evidence_based_completion_count"] == 3
    with Session(clean_postgres) as session:
        assert session.scalar(select(func.count()).select_from(ActionRecord)) == 9
    assert len(list((tmp_path / "blob-store" / "blobs").iterdir())) == 6


@pytest.mark.postgres
def test_task_suite_rejects_model_completion_with_wrong_artifact_and_no_read(
    clean_postgres: Engine, tmp_path: Path
) -> None:
    fixture = live_task_fixtures()[0]
    context = TrustedContext(
        "tenant-p08-false-complete",
        "subject-p08-false-complete",
        frozenset({"runs:read", "workspace:read", "artifacts:write", "artifacts:read"}),
    )
    calls = 0

    def provider() -> OpenAIResponsesProvider:
        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            task_context = json.loads(json.loads(request.content)["input"])["context"]
            if calls == 1:
                turn = {
                    "public_output": "",
                    "operations": [
                        {
                            "method": "POST",
                            "target": "/v1/artifacts",
                            "query": [],
                            "headers": {"content-type": "application/octet-stream"},
                            "payload": {
                                "kind": "json",
                                "value": {
                                    "content_base64": base64.b64encode(b"WRONG\n").decode(),
                                    "media_type": "text/plain",
                                },
                            },
                        }
                    ],
                    "final_candidate": None,
                    "request_input": None,
                }
            else:
                action = task_context["observations"][0]
                turn = {
                    "public_output": "Done.",
                    "operations": [],
                    "final_candidate": {
                        "answer": "Done.",
                        "artifact_ids": [action["result"]["value"]["id"]],
                        "evidence_ids": [action["action_id"]],
                        "acceptance_claims": ["claimed exact output"],
                    },
                    "request_input": None,
                }
            return httpx.Response(
                200,
                json={
                    "id": f"mock-false-completion-{calls}",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": json.dumps(turn)}],
                        }
                    ],
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                },
            )

        return OpenAIResponsesProvider(
            api_key="mock-only",
            model="controlled-model",
            client=httpx.Client(transport=httpx.MockTransport(respond)),
        )

    executor = LiveTaskExecutor(
        clean_postgres,
        context,
        provider,
        (fixture,),
        campaign_id="false-completion",
    )
    executor.seed(
        TrustedContext(
            context.tenant_id,
            context.subject_id,
            context.scopes | frozenset({"workspace:write"}),
        )
    )
    controls = EvaluationControls(
        "controlled-model",
        {},
        "workspace.file.read:1+artifact.create:1",
        "dev-1",
        tuple(sorted(context.scopes)),
        {},
        30.0,
        stable_hash([asdict(fixture)]),
    )
    rows = TaskSuiteRunner(executor, tmp_path / "false.raw.jsonl").run(executor.cases(), controls)
    assert calls == 2
    assert rows[0]["outcome"] == "succeeded"
    assert rows[0]["claimed_complete"] is True
    assert rows[0]["verified_completion"] is False
    assert rows[0]["accepted_evidence"] == []
    assert len(rows[0]["action_ids"]) == 1
    assert len(rows[0]["artifact_ids"]) == 1
    summary = summarize(rows)
    assert summary["design_complete"] is True
    assert summary["false_completion_count"] == 1
    assert summary["evidence_based_completion_count"] == 0


@pytest.mark.postgres
def test_task_suite_rejects_correct_artifact_created_before_read_observation(
    clean_postgres: Engine, tmp_path: Path
) -> None:
    fixture = live_task_fixtures()[0]
    context = TrustedContext(
        "tenant-p08-premature",
        "subject-p08-premature",
        frozenset({"runs:read", "workspace:read", "artifacts:write", "artifacts:read"}),
    )
    calls = 0

    def provider() -> OpenAIResponsesProvider:
        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            task_context = json.loads(json.loads(request.content)["input"])["context"]
            observations = task_context["observations"]
            if not observations:
                turn = {
                    "public_output": "",
                    "operations": [
                        {
                            "method": "GET",
                            "target": "/v1/workspaces/ws-p08-premature/files/upper.txt",
                            "query": [],
                            "headers": {},
                            "payload": None,
                        },
                        {
                            "method": "POST",
                            "target": "/v1/artifacts",
                            "query": [],
                            "headers": {"content-type": "application/octet-stream"},
                            "payload": {
                                "kind": "json",
                                "value": {
                                    "content_base64": base64.b64encode(
                                        fixture.expected_text.encode()
                                    ).decode(),
                                    "media_type": "text/plain",
                                },
                            },
                        },
                    ],
                    "final_candidate": None,
                    "request_input": None,
                }
            else:
                artifact_id = observations[1]["result"]["value"]["id"]
                turn = {
                    "public_output": "Done.",
                    "operations": [],
                    "final_candidate": {
                        "answer": "Done.",
                        "artifact_ids": [artifact_id],
                        "evidence_ids": [item["action_id"] for item in observations],
                        "acceptance_claims": ["exact output"],
                    },
                    "request_input": None,
                }
            return httpx.Response(
                200,
                json={
                    "id": f"mock-premature-{calls}",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": json.dumps(turn)}],
                        }
                    ],
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                },
            )

        return OpenAIResponsesProvider(
            api_key="mock-only",
            model="controlled-model",
            client=httpx.Client(transport=httpx.MockTransport(respond)),
        )

    executor = LiveTaskExecutor(
        clean_postgres,
        context,
        provider,
        (fixture,),
        campaign_id="premature",
    )
    executor.seed(
        TrustedContext(
            context.tenant_id,
            context.subject_id,
            context.scopes | frozenset({"workspace:write"}),
        )
    )
    controls = EvaluationControls(
        "controlled-model",
        {},
        "workspace.file.read:1+artifact.create:1",
        "dev-1",
        tuple(sorted(context.scopes)),
        {},
        30.0,
        stable_hash([asdict(fixture)]),
    )
    rows = TaskSuiteRunner(executor, tmp_path / "premature.raw.jsonl").run(
        executor.cases(), controls
    )
    assert calls == 2
    assert rows[0]["outcome"] == "succeeded"
    assert rows[0]["claimed_complete"] is True
    assert rows[0]["verified_completion"] is False
    assert len(rows[0]["action_ids"]) == 2
    assert summarize(rows)["false_completion_count"] == 1


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("timeout", "expected_outcome", "expected_reason"),
    [
        (False, "blocked", "provider_unavailable"),
        (True, "timeout", "provider timeout"),
    ],
)
def test_task_suite_provider_failure_or_timeout_blocks_run_and_preserves_raw_result(
    clean_postgres: Engine,
    tmp_path: Path,
    timeout: bool,
    expected_outcome: str,
    expected_reason: str,
) -> None:
    fixture = live_task_fixtures()[0]
    context = TrustedContext(
        "tenant-p08-task-failure",
        "subject-p08-task-failure",
        frozenset({"runs:read", "workspace:read", "artifacts:write", "artifacts:read"}),
    )

    class TimeoutProvider(ScriptedProvider):
        def generate(
            self,
            context: ContextSnapshot,
            tools: list[dict[str, Any]],
            limits: ModelLimits,
        ) -> CompleteModelResponse:
            del context, tools, limits
            raise ProviderUnavailable("provider timed out") from httpx.ReadTimeout("test timeout")

    def provider() -> ModelProvider:
        provider_type = TimeoutProvider if timeout else ScriptedProvider
        return provider_type([], model_revision="controlled-model")

    executor = LiveTaskExecutor(
        clean_postgres,
        context,
        provider,
        (fixture,),
        campaign_id="task-failure",
    )
    executor.seed(
        TrustedContext(
            context.tenant_id,
            context.subject_id,
            context.scopes | frozenset({"workspace:write"}),
        )
    )
    controls = EvaluationControls(
        "controlled-model",
        {},
        "workspace.file.read:1+artifact.create:1",
        "dev-1",
        tuple(sorted(context.scopes)),
        {"max_model_turns": 8, "max_tool_calls": 8, "max_output_tokens": 8192},
        30.0,
        stable_hash([asdict(fixture)]),
    )
    raw = tmp_path / "provider-failure.raw.jsonl"
    rows = TaskSuiteRunner(executor, raw).run(executor.cases(), controls)
    assert len(rows) == 1
    assert rows[0]["outcome"] == expected_outcome
    assert rows[0]["failure_reason"] == expected_reason
    assert rows[0]["verified_completion"] is False
    assert rows[0]["run_id"] is not None
    assert raw.read_text().count("\n") == 1
    run = RunController(clean_postgres).get_run(context, str(rows[0]["run_id"]))
    assert run.status == RunStatus.BLOCKED
    assert run.failure == {"code": "evaluation_stopped", "detail": expected_reason}
    assert summarize(rows)[f"{expected_outcome}_count"] == 1


@pytest.mark.postgres
def test_task_call_returning_after_deadline_is_recorded_as_timeout(
    clean_postgres: Engine, tmp_path: Path
) -> None:
    clock = [100.0]
    fixture = live_task_fixtures()[0]
    context = TrustedContext(
        "tenant-p08-task-late",
        "subject-p08-task-late",
        frozenset({"runs:read", "workspace:read", "artifacts:write", "artifacts:read"}),
    )

    class SlowProvider(ScriptedProvider):
        def generate(
            self,
            context: ContextSnapshot,
            tools: list[dict[str, Any]],
            limits: ModelLimits,
        ) -> CompleteModelResponse:
            clock[0] += 1.0
            return super().generate(context, tools, limits)

    provider = SlowProvider([{"operations": []}], model_revision="controlled-model")
    executor = LiveTaskExecutor(
        clean_postgres,
        context,
        lambda: provider,
        (fixture,),
        campaign_id="task-late-return",
        monotonic_clock=lambda: clock[0],
    )
    executor.seed(
        TrustedContext(
            context.tenant_id,
            context.subject_id,
            context.scopes | frozenset({"workspace:write"}),
        )
    )
    controls = EvaluationControls(
        "controlled-model",
        {},
        "workspace.file.read:1+artifact.create:1",
        "dev-1",
        tuple(sorted(context.scopes)),
        {"max_model_turns": 8, "max_tool_calls": 8, "max_output_tokens": 8192},
        0.25,
        stable_hash([asdict(fixture)]),
    )
    rows = TaskSuiteRunner(executor, tmp_path / "late.raw.jsonl").run(executor.cases(), controls)
    assert provider.calls == 1
    assert rows[0]["outcome"] == "timeout"
    assert rows[0]["verified_completion"] is False
    assert rows[0]["failure_reason"] == "evaluation deadline exceeded"
    assert rows[0]["run_status"] == "blocked"
    assert rows[0]["model_call_ids"]
    run = RunController(clean_postgres).get_run(context, str(rows[0]["run_id"]))
    assert run.status == RunStatus.BLOCKED
    assert run.failure == {"code": "evaluation_stopped", "detail": "evaluation deadline exceeded"}


@pytest.mark.postgres
def test_task_completed_after_deadline_keeps_evidence_but_not_success_score(
    clean_postgres: Engine, tmp_path: Path
) -> None:
    fixture = live_task_fixtures()[0]
    context = TrustedContext(
        "tenant-p08-task-late-success",
        "subject-p08-task-late-success",
        frozenset({"runs:read", "workspace:read", "artifacts:write", "artifacts:read"}),
    )
    clock = [100.0]

    class LateSuccessProvider(ScriptedProvider):
        def generate(
            self,
            context: ContextSnapshot,
            tools: list[dict[str, Any]],
            limits: ModelLimits,
        ) -> CompleteModelResponse:
            del tools, limits
            self.calls += 1
            if self.calls == 1:
                output = {
                    "operations": [
                        {
                            "method": "GET",
                            "target": (
                                f"/v1/workspaces/{executor.workspace_id}/files/{fixture.file_name}"
                            ),
                        }
                    ]
                }
            elif self.calls == 2:
                output = {
                    "operations": [
                        {
                            "method": "POST",
                            "target": "/v1/artifacts",
                            "headers": {"content-type": "application/octet-stream"},
                            "payload": {
                                "kind": "json",
                                "value": {
                                    "content_base64": base64.b64encode(
                                        fixture.expected_text.encode()
                                    ).decode(),
                                    "media_type": "text/plain",
                                },
                            },
                        }
                    ]
                }
            else:
                clock[0] += 1.0
                observations = context.content["observations"]
                output = {
                    "final_candidate": {
                        "answer": "The transformed artifact is stored.",
                        "artifact_ids": [observations[1]["result"]["value"]["id"]],
                        "evidence_ids": [item["action_id"] for item in observations],
                        "acceptance_claims": ["exact transformed bytes stored"],
                    }
                }
            return CompleteModelResponse(
                provider_request_id=f"scripted-late-task-{self.calls}",
                raw_response={"output": output},
                output=output,
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )

    provider = LateSuccessProvider([], model_revision="controlled-model")
    executor = LiveTaskExecutor(
        clean_postgres,
        context,
        lambda: provider,
        (fixture,),
        campaign_id="task-late-success",
        monotonic_clock=lambda: clock[0],
    )
    executor.seed(
        TrustedContext(
            context.tenant_id,
            context.subject_id,
            context.scopes | frozenset({"workspace:write"}),
        )
    )
    controls = EvaluationControls(
        "controlled-model",
        {},
        "workspace.file.read:1+artifact.create:1",
        "dev-1",
        tuple(sorted(context.scopes)),
        {"max_model_turns": 8, "max_tool_calls": 8, "max_output_tokens": 8192},
        0.25,
        stable_hash([asdict(fixture)]),
    )
    rows = TaskSuiteRunner(executor, tmp_path / "late-success.raw.jsonl").run(
        executor.cases(), controls
    )
    assert provider.calls == 3
    assert rows[0]["outcome"] == "timeout"
    assert rows[0]["verified_completion"] is False
    assert rows[0]["claimed_complete"] is True
    assert rows[0]["accepted_evidence"] == list(executor.cases()[0].expected_evidence)
    assert rows[0]["failure_reason"] == "evaluation deadline exceeded"
    assert rows[0]["run_status"] == "succeeded"
    summary = summarize(rows)
    assert summary["timeout_count"] == 1
    assert summary["evidence_based_completion_count"] == 0
    assert summary["late_evidenced_claim_count"] == 1
    assert summary["false_completion_count"] == 0
    run = RunController(clean_postgres).get_run(context, str(rows[0]["run_id"]))
    assert run.status == RunStatus.SUCCEEDED
