from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session

from hnh.adapters.models.deepseek_responses import DeepSeekResponsesProvider
from hnh.adapters.models.scripted import ScriptedProvider
from hnh.adapters.postgres.models import (
    ActionRecord,
    BudgetReservationRecord,
    CompletionRecord,
    ContextSnapshotRecord,
    EventRecord,
    ModelCallAttemptRecord,
    ModelCallRecord,
)
from hnh.application.action_gateway import ActionGateway
from hnh.application.auth import DevelopmentAuthenticator, DevelopmentPrincipal
from hnh.application.capabilities import CapabilityRegistry
from hnh.application.completion import CompletionGate
from hnh.application.context import ContextBuilder
from hnh.application.resources import ResourceStore
from hnh.application.run_controller import RunController
from hnh.application.runner import Runner, RunnerHooks
from hnh.domain.errors import BudgetExhausted
from hnh.domain.identity import TrustedContext
from hnh.domain.states import RunStatus
from hnh.ports.models import FinalCandidate, ModelProvider
from hnh.transport.http.app import create_app

TOKEN = "p03-token"
SCOPES = frozenset(
    {
        "actions:read",
        "artifacts:read",
        "artifacts:write",
        "capabilities:read",
        "integrations:invoke",
        "runs:read",
        "runs:write",
        "workspace:read",
        "workspace:write",
    }
)
CONTEXT = TrustedContext("tenant-p03", "subject-p03", SCOPES)


def p03_authenticator() -> DevelopmentAuthenticator:
    return DevelopmentAuthenticator(
        {TOKEN: DevelopmentPrincipal(CONTEXT.tenant_id, CONTEXT.subject_id, SCOPES)}
    )


def auth_headers(**extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", **extra}


def admit_run(
    controller: RunController,
    key: str,
    *,
    goal: str = "perform the requested work",
    workspace_id: str | None = "workspace-p03",
    limits: dict[str, int] | None = None,
) -> str:
    return controller.admit_run(
        CONTEXT,
        {
            "agent_id": "agent-p03",
            "input": goal,
            "workspace_id": workspace_id,
            "limits": limits
            or {
                "max_model_turns": 8,
                "max_tool_calls": 8,
                "max_output_tokens": 8192,
            },
        },
        key,
    ).resource_id


def build_runner(
    engine: Engine,
    provider: ModelProvider,
    *,
    max_format_repairs: int = 2,
    max_inline_chars: int = 4096,
) -> tuple[Runner, RunController, ActionGateway, ResourceStore]:
    controller = RunController(engine)
    registry = CapabilityRegistry()
    resources = ResourceStore(engine)
    gateway = ActionGateway(engine, registry, resources, controller)
    runner = Runner(
        controller,
        ContextBuilder(controller, registry, max_inline_chars=max_inline_chars),
        gateway,
        CompletionGate(engine, controller),
        provider,
        max_format_repairs=max_format_repairs,
    )
    return runner, controller, gateway, resources


def write_turn(path: str, text: str) -> dict[str, Any]:
    return {
        "public_output": "writing the requested file",
        "operations": [
            {
                "method": "PUT",
                "target": f"/v1/workspaces/workspace-p03/files/{path}",
                "query": [],
                "headers": {"content-type": "text/plain", "if-none-match": "*"},
                "payload": {"kind": "text", "text": text},
            }
        ],
        "final_candidate": None,
        "request_input": None,
    }


def final_turn(
    *, artifact_ids: list[str], evidence_ids: list[str], answer: str = "done"
) -> dict[str, Any]:
    return {
        "public_output": answer,
        "operations": [],
        "final_candidate": {
            "answer": answer,
            "artifact_ids": artifact_ids,
            "evidence_ids": evidence_ids,
            "acceptance_claims": ["requested output is backed by committed evidence"],
        },
        "request_input": None,
    }


def count(engine: Engine, model: type[Any], *, run_id: str | None = None) -> int:
    with Session(engine) as session:
        query = select(func.count()).select_from(model)
        if run_id is not None:
            query = query.where(model.run_id == run_id)
        return int(session.scalar(query) or 0)


@pytest.mark.postgres
def test_at_022_complete_model_output_is_committed_before_action_dispatch(
    clean_postgres: Engine,
) -> None:
    before_turn = write_turn("before.txt", "before crash recovery")
    before_provider = ScriptedProvider([before_turn, before_turn])
    before_runner, controller, before_gateway, resources = build_runner(
        clean_postgres, before_provider
    )
    before_run = admit_run(controller, "at-022-before")

    def crash_before_persist() -> None:
        raise RuntimeError("kill before model response commit")

    with pytest.raises(RuntimeError, match="before model response"):
        before_runner.advance(
            CONTEXT,
            before_run,
            hooks=RunnerHooks(after_provider_before_persist=crash_before_persist),
        )
    assert count(clean_postgres, ActionRecord, run_id=before_run) == 0
    with Session(clean_postgres) as session:
        call = session.scalar(select(ModelCallRecord).where(ModelCallRecord.run_id == before_run))
        assert call is not None
        assert call.status == "started"
        assert call.response_payload is None

    recovered_before = before_runner.advance(CONTEXT, before_run)
    assert recovered_before.status == RunStatus.RUNNING
    assert before_provider.calls == 2
    assert before_gateway.dispatch_count == 1
    assert count(clean_postgres, ActionRecord, run_id=before_run) == 1
    assert resources.read_file(CONTEXT, "workspace-p03", "before.txt").content == (
        b"before crash recovery"
    )

    after_turn = write_turn("after.txt", "after crash recovery")
    after_provider = ScriptedProvider([after_turn])
    after_runner, controller, after_gateway, resources = build_runner(
        clean_postgres, after_provider
    )
    after_run = admit_run(controller, "at-022-after")

    def crash_after_persist() -> None:
        raise RuntimeError("kill after model response commit")

    with pytest.raises(RuntimeError, match="after model response"):
        after_runner.advance(
            CONTEXT,
            after_run,
            hooks=RunnerHooks(after_persist_before_actions=crash_after_persist),
        )
    assert count(clean_postgres, ActionRecord, run_id=after_run) == 0
    with Session(clean_postgres) as session:
        call = session.scalar(select(ModelCallRecord).where(ModelCallRecord.run_id == after_run))
        assert call is not None
        assert call.status == "response_committed"
        assert call.response_payload is not None
        assert call.parsed_output == after_turn

    recovered_after = after_runner.advance(CONTEXT, after_run)
    assert recovered_after.status == RunStatus.RUNNING
    assert after_provider.calls == 1
    assert after_gateway.dispatch_count == 1
    assert count(clean_postgres, ActionRecord, run_id=after_run) == 1
    assert resources.read_file(CONTEXT, "workspace-p03", "after.txt").content == (
        b"after crash recovery"
    )


@pytest.mark.postgres
def test_at_023_invalid_model_output_repair_is_bounded(clean_postgres: Engine) -> None:
    provider = ScriptedProvider([{"invalid": 1}, {"invalid": 1}, {"invalid": 1}])
    runner, controller, _gateway, _resources = build_runner(
        clean_postgres, provider, max_format_repairs=2
    )
    run_id = admit_run(controller, "at-023")

    result = runner.run_to_terminal(CONTEXT, run_id, max_steps=10)

    assert result.status == RunStatus.FAILED
    assert result.failure is not None
    assert result.failure["code"] == "model_output_invalid"
    assert provider.calls == 2
    assert count(clean_postgres, ModelCallRecord, run_id=run_id) == 2
    assert count(clean_postgres, ModelCallAttemptRecord) == 2
    budget = controller.budget_status(CONTEXT, run_id)
    assert budget["used"]["model_turn"] == 2
    assert budget["used"]["output_tokens"] == 2


@pytest.mark.postgres
def test_at_024_completion_claim_cannot_replace_missing_artifact(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = admit_run(controller, "at-024")
    accepted_event = controller.get_event_history(CONTEXT, run_id, after=0, limit=10).events[0]
    provider = ScriptedProvider(
        [
            final_turn(
                artifact_ids=["artifact_missing"],
                evidence_ids=[accepted_event.event_id],
            )
        ]
    )
    runner, controller, _gateway, _resources = build_runner(clean_postgres, provider)

    result = runner.advance(CONTEXT, run_id)

    assert result.status == RunStatus.RUNNING
    assert result.final_answer is None
    with Session(clean_postgres) as session:
        completion = session.scalar(
            select(CompletionRecord).where(CompletionRecord.run_id == run_id)
        )
        assert completion is not None
        assert completion.status == "missing_evidence"
        assert "artifact:artifact_missing" in completion.missing_evidence


@pytest.mark.postgres
def test_at_025_acceptance_and_regression_results_remain_distinct(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = admit_run(controller, "at-025")
    controller.ensure_run_running(run_id, actor_ref="test")
    accepted_event = controller.get_event_history(CONTEXT, run_id, after=0, limit=10).events[0]
    gate = CompletionGate(clean_postgres, controller)

    result = gate.verify(
        CONTEXT,
        run_id,
        FinalCandidate(
            answer="the requested output is complete",
            evidence_ids=[accepted_event.event_id],
            acceptance_claims=["current request passed"],
        ),
        acceptance_results=[{"name": "current request", "passed": True}],
        regression_results=[{"name": "unrelated legacy check", "passed": False}],
    )

    assert result.verified is True
    assert result.acceptance_results == ({"name": "current request", "passed": True},)
    assert result.regression_results == ({"name": "unrelated legacy check", "passed": False},)
    assert controller.get_run(CONTEXT, run_id).status == RunStatus.SUCCEEDED
    with Session(clean_postgres) as session:
        completion = session.get(CompletionRecord, result.completion_id)
        assert completion is not None
        assert completion.acceptance_results[0]["passed"] is True
        assert completion.regression_results[0]["passed"] is False


@pytest.mark.postgres
def test_at_026_compacted_context_keeps_authorized_source_and_redacts_secret(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = admit_run(controller, "at-026")
    controller.ensure_run_running(run_id, actor_ref="test")
    action_id = controller.record_admitted_action(
        CONTEXT,
        run_id,
        turn_id=1,
        call_index=0,
        request_hash="a" * 64,
        capability_id="native.echo",
        capability_revision="1",
        effect_semantics="read_only",
    )
    assert controller.begin_action_dispatch(action_id) is True
    secret = "never-copy-this-secret"
    controller.complete_action(
        action_id,
        result={"api_key": secret, "large": "x" * 1000},
        succeeded=True,
        transport_kind="test",
        response_status=200,
    )
    context = ContextBuilder(controller, CapabilityRegistry(), max_inline_chars=96).build(
        CONTEXT, run_id, 1
    )

    encoded = json.dumps(context.content, ensure_ascii=False)
    assert secret not in encoded
    assert "[REDACTED]" in encoded
    assert context.source_refs == (
        {
            "kind": "action",
            "id": action_id,
            "url": f"/v1/actions/{action_id}",
            "version": 2,
            "request_hash": "a" * 64,
        },
    )
    assert controller.get_action(CONTEXT, action_id).result == {
        "api_key": secret,
        "large": "x" * 1000,
    }
    with Session(clean_postgres) as session:
        saved = session.scalar(
            select(ContextSnapshotRecord).where(ContextSnapshotRecord.run_id == run_id)
        )
        assert saved is not None
        assert saved.content_hash == context.content_hash


@pytest.mark.postgres
def test_at_027_sse_resume_starts_after_cursor_and_disconnect_does_not_cancel(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = admit_run(controller, "at-027")
    for index in range(2, 21):
        controller.append_event(
            run_id,
            "test.committed",
            actor_ref="test",
            data={"index": index},
        )
    app = create_app(
        engine=clean_postgres,
        authenticator=p03_authenticator(),
    )

    with TestClient(app) as client:
        with client.stream(
            "GET",
            f"/v1/runs/{run_id}/events",
            headers=auth_headers(**{"Last-Event-ID": "10"}),
        ) as response:
            body = "".join(response.iter_text())
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")

    ids = [int(line.removeprefix("id: ")) for line in body.splitlines() if line.startswith("id: ")]
    assert ids == list(range(11, 21))
    assert controller.get_run(CONTEXT, run_id).status == RunStatus.QUEUED


@pytest.mark.postgres
def test_at_028_expired_and_future_event_cursors_fail_before_streaming(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = admit_run(controller, "at-028")
    for index in range(2, 21):
        controller.append_event(
            run_id,
            "test.committed",
            actor_ref="test",
            data={"index": index},
        )
    with Session(clean_postgres) as session, session.begin():
        session.execute(
            delete(EventRecord).where(EventRecord.run_id == run_id, EventRecord.seq <= 10)
        )
    client = TestClient(create_app(engine=clean_postgres, authenticator=p03_authenticator()))

    expired = client.get(
        f"/v1/runs/{run_id}/events",
        headers=auth_headers(**{"Last-Event-ID": "5"}),
    )
    future = client.get(
        f"/v1/runs/{run_id}/events",
        headers=auth_headers(**{"Last-Event-ID": "21"}),
    )

    assert expired.status_code == 410
    assert expired.headers["content-type"].startswith("application/problem+json")
    assert expired.json()["code"] == "event_cursor_expired"
    assert expired.json()["history_url"].endswith("after=10")
    assert 'rel="history"' in expired.headers["link"]
    assert future.status_code == 409
    assert future.json()["code"] == "event_cursor_future"
    assert future.json()["current_sequence"] == 20
    assert future.json()["snapshot_url"] == f"/v1/runs/{run_id}"
    assert 'rel="snapshot"' in future.headers["link"]


@pytest.mark.postgres
def test_at_029_concurrent_budget_reservations_cannot_overdraw(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = admit_run(
        controller,
        "at-029",
        limits={
            "max_model_turns": 2,
            "max_tool_calls": 1,
            "max_output_tokens": 10,
        },
    )

    def reserve(index: int) -> str:
        try:
            return controller.reserve_budget(
                run_id,
                reservation_key=f"concurrent-tool-{index}",
                dimension="tool_call",
                amount=1,
            ).id
        except BudgetExhausted:
            return "exhausted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reserve, (1, 2)))

    assert outcomes.count("exhausted") == 1
    winner = next(item for item in outcomes if item != "exhausted")
    assert controller.budget_status(CONTEXT, run_id)["reserved"]["tool_call"] == 1
    controller.release_budget(winner)
    replacement = controller.reserve_budget(
        run_id,
        reservation_key="replacement-tool",
        dimension="tool_call",
        amount=1,
    )
    controller.settle_budget(replacement.id, actual_amount=1)
    output = controller.reserve_budget(
        run_id,
        reservation_key="output-upper-bound",
        dimension="output_tokens",
        amount=10,
    )
    controller.settle_budget(output.id, actual_amount=3)

    status = controller.budget_status(CONTEXT, run_id)
    assert status["used"]["tool_call"] == 1
    assert status["used"]["output_tokens"] == 3
    assert status["reserved"] == {}
    with Session(clean_postgres) as session:
        rows = session.scalars(
            select(BudgetReservationRecord).where(BudgetReservationRecord.run_id == run_id)
        ).all()
        assert sorted(row.status for row in rows) == [
            "released",
            "settled",
            "settled",
        ]


@pytest.mark.live
@pytest.mark.postgres
def test_at_030_live_model_read_transform_artifact_and_usage(
    clean_postgres: Engine,
) -> None:
    api_key = os.environ.get("HNH_DEEPSEEK_API_KEY")
    model = os.environ.get("HNH_DEEPSEEK_MODEL", "deepseek-flash")
    if os.environ.get("HNH_RUN_LIVE_TESTS") != "1" or not api_key:
        pytest.skip(
            "blocked_environment: set HNH_RUN_LIVE_TESTS=1 and HNH_DEEPSEEK_API_KEY for AT-030"
        )
    provider = DeepSeekResponsesProvider(
        api_key=api_key,
        model=model,
        base_url=os.environ.get("HNH_DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        reasoning_effort=os.environ.get("HNH_DEEPSEEK_REASONING_EFFORT", "none"),
        timeout_seconds=120,
        client=httpx.Client(timeout=120),
    )
    runner, controller, _gateway, resources = build_runner(clean_postgres, provider)
    resources.write_file(
        CONTEXT,
        "workspace-p03",
        "live-input.txt",
        b"live provider proof",
        "at-030-seed",
        if_match=None,
        if_none_match="*",
    )
    run_id = admit_run(
        controller,
        "at-030",
        goal=(
            "Read /v1/workspaces/workspace-p03/files/live-input.txt, convert the exact "
            "text to uppercase, save the uppercase bytes as a text/plain immutable artifact, "
            "then finish with that artifact and the successful read/create Action IDs as evidence."
        ),
        limits={
            "max_model_turns": 8,
            "max_tool_calls": 8,
            "max_output_tokens": 8192,
        },
    )

    result = runner.run_to_terminal(CONTEXT, run_id, max_steps=12)

    if result.status != RunStatus.SUCCEEDED:
        with Session(clean_postgres) as session:
            invalid_details = [
                event.data.get("detail")
                for event in session.scalars(
                    select(EventRecord)
                    .where(
                        EventRecord.run_id == run_id,
                        EventRecord.event_type == "model.output_invalid",
                    )
                    .order_by(EventRecord.seq)
                )
            ]
        pytest.fail(f"live Run failed: {result.failure}; invalid_details={invalid_details}")
    assert len(result.result_artifact_ids) == 1
    content, media_type, _etag = resources.get_artifact_content(
        CONTEXT, result.result_artifact_ids[0]
    )
    assert content == b"LIVE PROVIDER PROOF"
    assert media_type == "text/plain"
    with Session(clean_postgres) as session:
        calls = session.scalars(
            select(ModelCallRecord).where(ModelCallRecord.run_id == run_id)
        ).all()
        assert calls
        assert all(call.provider == "deepseek-responses" for call in calls)
        assert sum(int(call.usage.get("total_tokens", 0)) for call in calls) > 0
    evidence_output = os.environ.get("HNH_AT030_EVIDENCE_OUTPUT")
    if evidence_output:
        actions = controller.list_actions(CONTEXT, run_id)
        evidence = {
            "schema_version": 1,
            "recorded_at": datetime.now(UTC).isoformat(),
            "implementation_revision": os.environ.get(
                "HNH_EVAL_IMPLEMENTATION_REVISION", "unrecorded"
            ),
            "test": "AT-030",
            "provider": "deepseek-responses",
            "model": model,
            "reasoning_effort": os.environ.get("HNH_DEEPSEEK_REASONING_EFFORT", "none"),
            "run_status": result.status.value,
            "artifact": {
                "media_type": media_type,
                "sha256": sha256(content).hexdigest(),
                "size_bytes": len(content),
            },
            "action_capabilities": [action.capability_id for action in actions],
            "model_calls": len(calls),
            "usage": {
                key: sum(int(call.usage.get(key, 0)) for call in calls)
                for key in ("input_tokens", "output_tokens", "total_tokens")
            },
            "provider_request_hashes": [
                sha256(call.provider_request_id.encode()).hexdigest()
                for call in calls
                if call.provider_request_id
            ],
        }
        Path(evidence_output).write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
