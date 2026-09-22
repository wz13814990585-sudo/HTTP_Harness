from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from hnh.adapters.postgres.database import build_session_factory
from hnh.adapters.postgres.models import ActionRecord, ArtifactRecord, EventRecord, RunRecord
from hnh.application.run_controller import ClaimedJob, RunController
from hnh.domain.identity import TrustedContext
from hnh.domain.states import TERMINAL_RUN_STATUSES, ActionStatus
from hnh.ports.models import FinalCandidate


@dataclass(frozen=True, slots=True)
class GateResult:
    verified: bool
    completion_id: str
    missing_evidence: tuple[str, ...]
    acceptance_results: tuple[dict[str, Any], ...]
    regression_results: tuple[dict[str, Any], ...]


class CompletionGate:
    def __init__(self, engine: Engine, controller: RunController) -> None:
        self._sessions: sessionmaker[Session] = build_session_factory(engine)
        self.controller = controller

    def verify(
        self,
        context: TrustedContext,
        run_id: str,
        candidate: FinalCandidate,
        *,
        model_call_id: str | None = None,
        acceptance_results: list[dict[str, Any]] | None = None,
        regression_results: list[dict[str, Any]] | None = None,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> GateResult:
        run = self.controller.get_run(context, run_id)
        acceptance = list(acceptance_results or [])
        regression = list(regression_results or [])
        missing: list[str] = []
        with self._sessions() as session:
            for artifact_id in candidate.artifact_ids:
                artifact = session.scalar(
                    select(ArtifactRecord).where(
                        ArtifactRecord.id == artifact_id,
                        ArtifactRecord.tenant_id == context.tenant_id,
                        ArtifactRecord.subject_id == context.subject_id,
                    )
                )
                if artifact is None:
                    missing.append(f"artifact:{artifact_id}")
                    continue
                if artifact.source_action_id is None:
                    if artifact_id not in run.artifact_refs:
                        missing.append(f"artifact_scope:{artifact_id}")
                else:
                    source_run = session.scalar(
                        select(ActionRecord.run_id).where(
                            ActionRecord.id == artifact.source_action_id
                        )
                    )
                    if source_run != run_id:
                        missing.append(f"artifact_scope:{artifact_id}")
            for evidence_id in candidate.evidence_ids:
                action_status = session.scalar(
                    select(ActionRecord.status).where(
                        ActionRecord.id == evidence_id,
                        ActionRecord.run_id == run_id,
                        ActionRecord.tenant_id == context.tenant_id,
                        ActionRecord.subject_id == context.subject_id,
                    )
                )
                event_exists = session.scalar(
                    select(EventRecord.id).where(
                        EventRecord.id == evidence_id,
                        EventRecord.run_id == run_id,
                    )
                )
                if action_status != ActionStatus.SUCCEEDED.value and event_exists is None:
                    missing.append(f"evidence:{evidence_id}")
            unresolved = session.scalars(
                select(ActionRecord).where(
                    ActionRecord.run_id == run_id,
                    ActionRecord.status.not_in(
                        (
                            ActionStatus.SUCCEEDED.value,
                            ActionStatus.FAILED.value,
                            ActionStatus.CANCELLED.value,
                        )
                    ),
                )
            ).all()
            missing.extend(f"unresolved_action:{row.id}" for row in unresolved)
            children = session.scalars(
                select(RunRecord).where(
                    RunRecord.parent_run_id == run_id,
                    RunRecord.status.not_in({status.value for status in TERMINAL_RUN_STATUSES}),
                )
            ).all()
            missing.extend(f"unresolved_child:{row.id}" for row in children)
        if not candidate.evidence_ids:
            missing.append("evidence_ids")
        for result in acceptance:
            if result.get("passed") is not True:
                missing.append(f"acceptance:{result.get('name', 'unnamed')}")
        missing = sorted(set(missing))
        verified = not missing
        completion_id = self.controller.record_completion(
            context,
            run_id,
            model_call_id=model_call_id,
            candidate=candidate.model_dump(mode="json"),
            verified=verified,
            missing_evidence=missing,
            acceptance_results=acceptance,
            regression_results=regression,
            claimed_job=claimed_job,
            worker_id=worker_id,
        )
        return GateResult(
            verified,
            completion_id,
            tuple(missing),
            tuple(acceptance),
            tuple(regression),
        )
