"""A leased Runner may commit a complete turn; a stale one may not finish a Run."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session

from hnh.adapters.models.scripted import ScriptedProvider
from hnh.adapters.postgres.models import JobRecord
from hnh.application.action_gateway import ActionGateway
from hnh.application.capabilities import CapabilityRegistry
from hnh.application.completion import CompletionGate
from hnh.application.context import ContextBuilder
from hnh.application.resources import ResourceStore
from hnh.application.run_controller import ClaimedJob, RunController
from hnh.application.runner import Runner, RunnerHooks
from hnh.domain.errors import LeaseLost
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus, RunStatus

ACTOR = TrustedContext(
    "tenant-p08-runner",
    "subject-p08-runner",
    frozenset({"runs:read", "integrations:invoke"}),
)


def _runner(engine: Engine, controller: RunController, provider: ScriptedProvider) -> Runner:
    registry = CapabilityRegistry()
    return Runner(
        controller,
        ContextBuilder(controller, registry),
        ActionGateway(engine, registry, ResourceStore(engine), controller),
        CompletionGate(engine, controller),
        provider,
    )


def _expire(engine: Engine, claimed: ClaimedJob) -> None:
    with Session(engine) as session, session.begin():
        session.execute(
            update(JobRecord)
            .where(JobRecord.id == claimed.id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )


@pytest.mark.postgres
def test_leased_runner_dispatches_one_read_only_action_and_finishes(clean_postgres: Engine) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {
            "agent_id": "p08-leased",
            "input": "echo same-input then finish",
            "limits": {"max_model_turns": 2, "max_tool_calls": 1, "max_output_tokens": 2048},
        },
        "p08-leased-echo",
    ).resource_id
    claimed = controller.claim_job("worker-current", kind="advance_run")
    assert claimed is not None
    first_turn = {
        "public_output": "",
        "operations": [
            {
                "method": "POST",
                "target": "/v1/integrations/native/operations/echo/invocations",
                "headers": {"content-type": "application/json"},
                "payload": {"kind": "json", "value": {"message": "same-input"}},
            }
        ],
        "final_candidate": None,
        "request_input": None,
    }
    first = _runner(clean_postgres, controller, ScriptedProvider([first_turn]))
    assert first.advance(ACTOR, run_id, claimed_job=claimed, worker_id="worker-current").status == (
        RunStatus.RUNNING
    )
    actions = controller.list_actions(ACTOR, run_id)
    assert len(actions) == 1 and actions[0].status == ActionStatus.SUCCEEDED
    final_turn = {
        "public_output": "Echo observed.",
        "operations": [],
        "final_candidate": {
            "answer": "Echo observed.",
            "artifact_ids": [],
            "evidence_ids": [actions[0].id],
            "acceptance_claims": ["echo returned same-input"],
        },
        "request_input": None,
    }
    second = _runner(clean_postgres, controller, ScriptedProvider([final_turn]))
    completed = second.advance(ACTOR, run_id, claimed_job=claimed, worker_id="worker-current")
    assert completed.status == RunStatus.SUCCEEDED
    controller.complete_job(claimed, "worker-current")


@pytest.mark.postgres
def test_stale_runner_cannot_commit_completion_but_successor_reuses_response(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        ACTOR,
        {
            "agent_id": "p08-leased",
            "input": "finish with accepted event evidence",
            "limits": {"max_model_turns": 2, "max_tool_calls": 1, "max_output_tokens": 2048},
        },
        "p08-stale-completion",
    ).resource_id
    evidence_id = controller.get_recent_events(ACTOR, run_id)[0].event_id
    first_claim = controller.claim_job("worker-old", kind="advance_run")
    assert first_claim is not None
    turn = {
        "public_output": "done",
        "operations": [],
        "final_candidate": {
            "answer": "Done with committed evidence.",
            "artifact_ids": [],
            "evidence_ids": [evidence_id],
            "acceptance_claims": ["accepted event exists"],
        },
        "request_input": None,
    }
    old_provider = ScriptedProvider([turn])
    reclaimed: list[ClaimedJob] = []

    def lose_lease() -> None:
        _expire(clean_postgres, first_claim)
        successor = controller.claim_job("worker-new", kind="advance_run")
        assert successor is not None
        reclaimed.append(successor)

    with pytest.raises(LeaseLost):
        _runner(clean_postgres, controller, old_provider).advance(
            ACTOR,
            run_id,
            claimed_job=first_claim,
            worker_id="worker-old",
            hooks=RunnerHooks(after_persist_before_actions=lose_lease),
        )
    assert old_provider.calls == 1
    assert controller.get_run(ACTOR, run_id).status == RunStatus.RUNNING
    latest = controller.latest_model_call(ACTOR, run_id)
    assert latest is not None and latest.status == "response_committed"

    new_provider = ScriptedProvider([])
    completed = _runner(clean_postgres, controller, new_provider).advance(
        ACTOR,
        run_id,
        claimed_job=reclaimed[0],
        worker_id="worker-new",
    )
    assert completed.status == RunStatus.SUCCEEDED
    assert new_provider.calls == 0
