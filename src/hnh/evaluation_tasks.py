"""Opt-in real-model, multi-capability task suite; never used by the API worker."""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from hnh.adapters.blobs.filesystem import FileBlobStore
from hnh.adapters.models.openai_responses import OpenAIResponsesProvider
from hnh.adapters.postgres.database import build_engine
from hnh.adapters.postgres.models import EventRecord, ModelCallRecord
from hnh.application.action_gateway import ActionGateway
from hnh.application.capabilities import CapabilityRegistry
from hnh.application.completion import CompletionGate
from hnh.application.context import ContextBuilder
from hnh.application.operations import HttpOperation
from hnh.application.resources import ResourceStore
from hnh.application.run_controller import RunController
from hnh.application.runner import Runner
from hnh.domain.errors import HarnessError
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus, RunStatus
from hnh.evaluation import (
    EvaluationCase,
    EvaluationControls,
    EvaluationDesignError,
    EvaluationResult,
    Outcome,
    TaskSuiteRunner,
    stable_hash,
)
from hnh.ports.models import ModelProvider

ModelFactory = Callable[[], ModelProvider]


@dataclass(frozen=True, slots=True)
class TaskFixture:
    case_id: str
    file_name: str
    input_text: str
    instruction: str
    expected_text: str

    def evaluation_case(self, workspace_id: str) -> EvaluationCase:
        target = f"/v1/workspaces/{workspace_id}/files/{self.file_name}"
        return EvaluationCase(
            self.case_id,
            f"Read the exact contents of {target}. {self.instruction} "
            "Create one immutable text/plain artifact containing the exact UTF-8 answer. "
            "Finish with its artifact ID and the committed read/create Action IDs as evidence. "
            "Do not invent observations, artifact IDs, or evidence.",
            (f"artifact-sha256:{hashlib.sha256(self.expected_text.encode()).hexdigest()}",),
        )


def live_task_fixtures() -> tuple[TaskFixture, ...]:
    """Frozen file-read → transform → artifact-write tasks with exact byte oracles."""
    return (
        TaskFixture(
            "transform-upper-001",
            "upper.txt",
            "paper boat\n",
            "Convert every letter to uppercase, preserving the final LF newline.",
            "PAPER BOAT\n",
        ),
        TaskFixture(
            "transform-lines-002",
            "lines.txt",
            "alpha\n\nbeta\n",
            "Count nonempty lines and write the decimal count followed by one LF newline.",
            "2\n",
        ),
        TaskFixture(
            "transform-sum-003",
            "numbers.csv",
            "4,9,13\n",
            "Sum all comma-separated integers and write the decimal sum "
            "followed by one LF newline.",
            "26\n",
        ),
    )


class LiveTaskExecutor:
    """One durable Run per task; grades committed actions and stored artifact bytes."""

    def __init__(
        self,
        engine: Engine,
        context: TrustedContext,
        model_factory: ModelFactory,
        fixtures: tuple[TaskFixture, ...],
        *,
        campaign_id: str,
        blob_root: Path | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        if (
            not campaign_id
            or not fixtures
            or len({item.case_id for item in fixtures}) != len(fixtures)
        ):
            raise ValueError("task campaign and unique fixtures are required")
        self.engine = engine
        self.context = context
        self.model_factory = model_factory
        self.fixtures = {item.case_id: item for item in fixtures}
        self.campaign_id = campaign_id
        self._clock = monotonic_clock or time.monotonic
        self.workspace_id = f"ws-p08-{campaign_id[:16]}"
        self.resources = ResourceStore(
            engine, FileBlobStore(blob_root) if blob_root is not None else None
        )
        builtin = CapabilityRegistry()
        self.registry = CapabilityRegistry(
            (
                builtin.get(context, "workspace.file.read"),
                builtin.get(context, "artifact.create"),
            )
        )

    def cases(self) -> tuple[EvaluationCase, ...]:
        return tuple(
            fixture.evaluation_case(self.workspace_id) for fixture in self.fixtures.values()
        )

    def preflight(self, controls: EvaluationControls) -> None:
        """Reject mislabeled controls before fixture writes or model execution."""
        read = self.registry.get(self.context, "workspace.file.read")
        create = self.registry.get(self.context, "artifact.create")
        capability_revision = f"{read.id}:{read.revision}+{create.id}:{create.revision}"
        if controls.capability_revision != capability_revision:
            raise EvaluationDesignError("task suite capability revision differs from controls")
        if controls.fixtures_hash != stable_hash([asdict(item) for item in self.fixtures.values()]):
            raise EvaluationDesignError("task suite fixtures differ from controls")
        if controls.scopes != tuple(sorted(self.context.scopes)):
            raise EvaluationDesignError("task suite scopes differ from controls")
        if controls.policy_revision != "dev-1":
            raise EvaluationDesignError("task suite policy revision differs from controls")

    def seed(self, seed_context: TrustedContext) -> None:
        """Admit trusted fixture writes through the same policy gateway."""
        if (
            seed_context.tenant_id != self.context.tenant_id
            or seed_context.subject_id != self.context.subject_id
            or "workspace:write" not in seed_context.scopes
        ):
            raise ValueError("fixture seeding requires the same actor with workspace:write")
        seed_registry = CapabilityRegistry(
            (CapabilityRegistry().get(seed_context, "workspace.file.write"),)
        )
        gateway = ActionGateway(
            self.engine,
            seed_registry,
            self.resources,
            RunController(self.engine),
        )
        for fixture in self.fixtures.values():
            outcome = gateway.execute(
                seed_context,
                HttpOperation.model_validate(
                    {
                        "method": "PUT",
                        "target": (f"/v1/workspaces/{self.workspace_id}/files/{fixture.file_name}"),
                        "headers": {"content-type": "text/plain", "if-none-match": "*"},
                        "payload": {"kind": "text", "text": fixture.input_text},
                    }
                ),
                idempotency_key=f"eval-seed-{self.campaign_id}-{fixture.case_id}",
                transport_kind="inproc",
            )
            if not outcome.succeeded:
                raise RuntimeError("trusted fixture write did not complete")

    def run(self, case: EvaluationCase, controls: EvaluationControls) -> EvaluationResult:
        self.preflight(controls)
        fixture = self.fixtures.get(case.case_id)
        if fixture is None or case != fixture.evaluation_case(self.workspace_id):
            raise EvaluationDesignError("task case does not match the frozen fixture")
        provider = self.model_factory()
        if provider.model_revision != controls.model:
            raise ValueError("provider model differs from recorded evaluation control")
        controller = RunController(self.engine)
        gateway = ActionGateway(self.engine, self.registry, self.resources, controller)
        runner = Runner(
            controller,
            ContextBuilder(controller, self.registry),
            gateway,
            CompletionGate(self.engine, controller),
            provider,
            default_max_output_tokens=min(1024, controls.budget.get("max_output_tokens", 8192)),
        )
        admitted = controller.admit_run(
            self.context,
            {
                "agent_id": "p08-live-task-suite",
                "input": case.task,
                "workspace_id": self.workspace_id,
                "limits": {
                    "max_model_turns": controls.budget.get("max_model_turns", 8),
                    "max_tool_calls": controls.budget.get("max_tool_calls", 8),
                    "max_output_tokens": controls.budget.get("max_output_tokens", 8192),
                    "max_wall_seconds": max(1, int(controls.timeout_seconds)),
                },
            },
            f"eval-task-{self.campaign_id}-{case.case_id}",
        )
        if admitted.replayed:
            raise ValueError("task evaluation campaign already admitted this case")
        run_id = admitted.resource_id
        deadline = self._clock() + controls.timeout_seconds
        failure_reason: str | None = None
        timed_out = False
        for _ in range(12):
            if self._clock() >= deadline:
                failure_reason = "evaluation deadline exceeded"
                timed_out = True
                break
            try:
                snapshot = runner.advance(self.context, run_id)
            except HarnessError as exc:
                provider_timeout = isinstance(exc.__cause__, httpx.TimeoutException | TimeoutError)
                timed_out = provider_timeout or self._clock() >= deadline
                failure_reason = (
                    "provider timeout"
                    if provider_timeout
                    else "evaluation deadline exceeded"
                    if timed_out
                    else exc.code
                )
                break
            except TimeoutError:
                failure_reason = "executor timeout"
                timed_out = True
                break
            except Exception as exc:
                timed_out = self._clock() >= deadline
                failure_reason = (
                    "evaluation deadline exceeded"
                    if timed_out
                    else f"executor error: {type(exc).__name__}"
                )
                break
            if self._clock() >= deadline:
                failure_reason = "evaluation deadline exceeded"
                timed_out = True
                break
            if snapshot.status != RunStatus.RUNNING:
                break
        else:
            failure_reason = "evaluation advancement limit exceeded"
        snapshot = controller.get_run(self.context, run_id)
        if failure_reason is not None and snapshot.status == RunStatus.RUNNING:
            # The task suite permits local transactional artifact writes. A
            # stopped evaluator must not leave its Run dispatchable, nor claim
            # a clean failure when an Action may need reconciliation.
            snapshot = controller.block_run(
                run_id,
                code="evaluation_stopped",
                detail=failure_reason,
            )
        actions = controller.list_actions(self.context, run_id)
        reads = [
            action
            for action in actions
            if action.capability_id == "workspace.file.read"
            and action.status == ActionStatus.SUCCEEDED
            and action.bound_operation.get("workspace_id") == self.workspace_id
            and action.bound_operation.get("file_path") == fixture.file_name
        ]
        creates = [
            action
            for action in actions
            if action.capability_id == "artifact.create" and action.status == ActionStatus.SUCCEEDED
        ]
        evidence: list[str] = []
        if reads and creates and len(snapshot.result_artifact_ids) == 1:
            artifact_id = snapshot.result_artifact_ids[0]
            matching_create = any(
                isinstance(action.result, dict)
                and isinstance(action.result.get("value"), dict)
                and action.result["value"].get("id") == artifact_id
                # A read in the same model response cannot have informed the
                # proposed artifact. Require a later model decision turn.
                and any(read.turn_id < action.turn_id for read in reads)
                for action in creates
            )
            if matching_create:
                try:
                    content, media_type, _etag = self.resources.get_artifact_content(
                        self.context, artifact_id
                    )
                except HarnessError:
                    pass
                else:
                    if content == fixture.expected_text.encode() and media_type == "text/plain":
                        evidence.append(f"artifact-sha256:{hashlib.sha256(content).hexdigest()}")
        with Session(self.engine) as session:
            calls = session.scalars(
                select(ModelCallRecord)
                .where(ModelCallRecord.run_id == run_id)
                .order_by(ModelCallRecord.turn_id)
            ).all()
            event_start = session.scalar(
                select(func.min(EventRecord.seq)).where(EventRecord.run_id == run_id)
            )
        reported = bool(calls) and all(
            type(call.usage.get("input_tokens")) is int
            and type(call.usage.get("output_tokens")) is int
            and call.usage["input_tokens"] >= 0
            and call.usage["output_tokens"] >= 0
            for call in calls
        )
        claimed = any(
            isinstance(call.parsed_output, dict)
            and call.parsed_output.get("final_candidate") is not None
            for call in calls
        )
        outcome: Outcome
        if timed_out:
            outcome = "timeout"
        elif snapshot.status == RunStatus.SUCCEEDED:
            outcome = "succeeded"
        elif snapshot.status in {RunStatus.RUNNING, RunStatus.BLOCKED, RunStatus.WAITING_INPUT}:
            outcome = "blocked"
        else:
            outcome = "failed"
        return EvaluationResult(
            run_id,
            outcome,
            tuple(evidence),
            claimed,
            sum(call.usage["input_tokens"] for call in calls) if reported else None,
            sum(call.usage["output_tokens"] for call in calls) if reported else None,
            "provider_reported" if reported else "unavailable",
            failure_reason=failure_reason or (snapshot.failure or {}).get("code"),
            model_call_ids=tuple(call.id for call in calls),
            action_ids=tuple(action.id for action in actions),
            artifact_ids=snapshot.result_artifact_ids,
            event_seq_start=event_start,
            event_seq_end=snapshot.event_seq,
            run_status=snapshot.status.value,
        )


@dataclass(frozen=True, slots=True)
class LiveTaskConfig:
    database_url: str
    api_key: str
    model: str
    output: Path
    tenant_id: str
    subject_id: str
    implementation_revision: str
    blob_root: Path | None = None

    @classmethod
    def from_environment(cls) -> LiveTaskConfig:
        names = (
            "HNH_DATABASE_URL",
            "HNH_OPENAI_API_KEY",
            "HNH_OPENAI_MODEL",
            "HNH_EVAL_TASK_OUTPUT",
            "HNH_EVAL_TENANT",
            "HNH_EVAL_SUBJECT",
            "HNH_EVAL_IMPLEMENTATION_REVISION",
        )
        missing = [name for name in names if not os.environ.get(name)]
        if missing:
            raise ValueError(f"live task evaluation configuration missing: {', '.join(missing)}")
        values = [os.environ[name] for name in names]
        output = Path(values[3]).resolve()
        if output.exists():
            raise FileExistsError("live task raw JSONL destination already exists")
        if re.fullmatch(r"[A-Za-z0-9._:/@+-]{1,200}", values[6]) is None:
            raise ValueError("HNH_EVAL_IMPLEMENTATION_REVISION has an invalid format")
        blob_root = os.environ.get("HNH_EVAL_BLOB_ROOT")
        return cls(
            database_url=values[0],
            api_key=values[1],
            model=values[2],
            output=output,
            tenant_id=values[4],
            subject_id=values[5],
            implementation_revision=values[6],
            blob_root=Path(blob_root).resolve() if blob_root else None,
        )


def run_live_tasks(config: LiveTaskConfig) -> Path:
    """Operator-invoked run on a dedicated migrated database, never a CI mock."""
    context = TrustedContext(
        config.tenant_id,
        config.subject_id,
        frozenset({"runs:read", "workspace:read", "artifacts:write", "artifacts:read"}),
    )
    fixtures = live_task_fixtures()
    controls = EvaluationControls(
        model=config.model,
        model_settings={"max_output_tokens_per_turn": 1024, "stream": False},
        capability_revision="workspace.file.read:1+artifact.create:1",
        policy_revision="dev-1",
        scopes=tuple(sorted(context.scopes)),
        budget={"max_model_turns": 8, "max_tool_calls": 8, "max_output_tokens": 8192},
        timeout_seconds=300.0,
        fixtures_hash=stable_hash([asdict(item) for item in fixtures]),
        environment_hash=stable_hash(
            {
                "implementation_revision": config.implementation_revision,
                "model_adapter": "openai-responses",
                "blob_backend": "filesystem" if config.blob_root is not None else "postgresql",
                "suite": "p08-live-tasks-v1",
            }
        ),
        evidence_tier="live_provider",
    )
    engine = build_engine(config.database_url)
    try:
        executor = LiveTaskExecutor(
            engine,
            context,
            lambda: OpenAIResponsesProvider(api_key=config.api_key, model=config.model),
            fixtures,
            campaign_id=uuid4().hex,
            blob_root=config.blob_root,
        )
        executor.preflight(controls)
        executor.seed(
            TrustedContext(
                context.tenant_id,
                context.subject_id,
                context.scopes | frozenset({"workspace:write"}),
            )
        )
        TaskSuiteRunner(executor, config.output).run(executor.cases(), controls)
    finally:
        engine.dispose()
    return config.output
