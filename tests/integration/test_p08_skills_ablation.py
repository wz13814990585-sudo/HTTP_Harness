"""Real PostgreSQL skills off/on wiring with mock Responses; no live benefit claim."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from hnh.adapters.models.openai_responses import OpenAIResponsesProvider
from hnh.adapters.postgres.models import RunRecord
from hnh.application.run_controller import RunController
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus
from hnh.evaluation import AblationRunner, EvaluationControls, EvaluationDesignError, summarize
from hnh.evaluation_live import live_echo_cases
from hnh.evaluation_skills import SKILL_FILE, SKILL_ID, LiveSkillsAblationExecutor


def _mock_provider(
    cases: tuple[Any, ...], first_requests: list[dict[str, Any]]
) -> OpenAIResponsesProvider:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        task_context = json.loads(body["input"])["context"]
        expected = next(
            case.expected_evidence[0].removeprefix("echo:")
            for case in cases
            if case.task == task_context["goal"]
        )
        if calls == 1:
            first_requests.append(body)
            turn = {
                "public_output": "",
                "operations": [
                    {
                        "method": "POST",
                        "target": "/v1/integrations/native/operations/echo/invocations",
                        "query": [],
                        "headers": {"content-type": "application/json"},
                        "payload": {"kind": "json", "value": {"message": expected}},
                    }
                ],
                "final_candidate": None,
                "request_input": None,
            }
        else:
            action_id = task_context["observations"][0]["action_id"]
            turn = {
                "public_output": "Echo observed.",
                "operations": [],
                "final_candidate": {
                    "answer": "Echo observed.",
                    "artifact_ids": [],
                    "evidence_ids": [action_id],
                    "acceptance_claims": [f"echo returned {expected}"],
                },
                "request_input": None,
            }
        return httpx.Response(
            200,
            json={
                "id": f"mock-skills-{calls}",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(turn)}],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    return OpenAIResponsesProvider(
        api_key="mock-only",
        model="controlled-model",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )


def _controls(executor: LiveSkillsAblationExecutor, context: TrustedContext) -> EvaluationControls:
    return EvaluationControls(
        model="controlled-model",
        model_settings={"max_output_tokens_per_turn": 1024, "stream": False},
        capability_revision=executor.capability_revision,
        policy_revision="dev-1",
        scopes=tuple(sorted(context.scopes)),
        budget={"max_model_turns": 3, "max_tool_calls": 1, "max_output_tokens": 2048},
        timeout_seconds=30.0,
        fixtures_hash=executor.fixtures_hash,
    )


@pytest.mark.postgres
def test_skills_ablation_toggles_only_allowlisted_context_and_keeps_gateway(
    clean_postgres: Engine, tmp_path: Path
) -> None:
    (tmp_path / SKILL_FILE).write_text("Preserve Unicode and punctuation exactly.\n")
    context = TrustedContext(
        "tenant-p08-skills",
        "subject-p08-skills",
        frozenset({"runs:read", "integrations:invoke"}),
    )
    cases = live_echo_cases()[:2]
    first_requests: list[dict[str, Any]] = []
    executor = LiveSkillsAblationExecutor(
        clean_postgres,
        context,
        lambda: _mock_provider(cases, first_requests),
        cases,
        tmp_path,
        campaign_id="skills-local",
    )
    controls = _controls(executor, context)
    executor.preflight(controls)
    rows = AblationRunner(executor, tmp_path / "skills.raw.jsonl").run(cases, controls, "skills")
    assert len(rows) == 4
    assert len(first_requests) == 4
    assert all(body["model"] == "controlled-model" for body in first_requests)
    assert len({body["max_output_tokens"] for body in first_requests}) == 1
    assert all(
        json.loads(body["input"])["capabilities"]
        == json.loads(first_requests[0]["input"])["capabilities"]
        for body in first_requests
    )
    assert all(row["verified_completion"] is True for row in rows)
    assert all(row["model_input_tokens"] == 20 for row in rows)
    assert all(row["model_output_tokens"] == 10 for row in rows)
    assert len({row["controls_hash"] for row in rows}) == 1
    assert [row["feature_enabled"] for row in rows] == [False, True, True, False]
    controller = RunController(clean_postgres)
    for row in rows:
        run_id = row["run_id"]
        assert isinstance(run_id, str)
        snapshot = controller.get_context_snapshot(context, run_id, 1)
        skills = snapshot.content["skills"]
        if row["feature_enabled"]:
            assert len(skills) == 1
            assert skills[0]["id"] == SKILL_ID
            assert skills[0]["revision"] == executor.skill_ref["revision"]
            assert any(ref.get("kind") == "skill" for ref in snapshot.source_refs)
        else:
            assert skills == []
            assert all(ref.get("kind") != "skill" for ref in snapshot.source_refs)
        actions = controller.list_actions(context, run_id)
        assert len(actions) == 1
        assert actions[0].capability_id == "native.echo"
        assert actions[0].status == ActionStatus.SUCCEEDED
    summary = summarize(rows)
    assert summary["design_complete"] is True
    assert summary["by_condition"]["off"]["denominator"] == 2
    assert summary["by_condition"]["on"]["denominator"] == 2


@pytest.mark.postgres
def test_skills_ablation_rejects_changed_skill_before_model_or_run(
    clean_postgres: Engine, tmp_path: Path
) -> None:
    skill_path = tmp_path / SKILL_FILE
    skill_path.write_text("Initial pinned guidance.\n")
    context = TrustedContext(
        "tenant-p08-skill-drift",
        "subject-p08-skill-drift",
        frozenset({"runs:read", "integrations:invoke"}),
    )
    cases = (live_echo_cases()[0],)
    executor = LiveSkillsAblationExecutor(
        clean_postgres,
        context,
        lambda: pytest.fail("model must not run after skill drift"),
        cases,
        tmp_path,
        campaign_id="skill-drift",
    )
    controls = _controls(executor, context)
    skill_path.write_text("Guidance changed after pinning.\n")
    with pytest.raises(EvaluationDesignError, match="skill changed"):
        executor.preflight(controls)
    with pytest.raises(EvaluationDesignError, match="skill changed"):
        executor.run(cases[0], controls, "skills", True)
    with Session(clean_postgres) as session:
        assert session.scalar(select(func.count()).select_from(RunRecord)) == 0
