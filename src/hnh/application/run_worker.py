from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Thread

from hnh.application.run_controller import ClaimedJob, RunController
from hnh.application.runner import Runner
from hnh.domain.errors import (
    BudgetExhausted,
    DependencyUnavailable,
    HarnessError,
    LeaseLost,
    MigrationRequired,
    ProviderUnavailable,
    VersionConflict,
)
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus, RunStatus

ActorResolver = Callable[[str, str], TrustedContext | None]


@dataclass(frozen=True, slots=True)
class WorkerStep:
    run_id: str
    disposition: str


class RunWorker:
    """Bounded, lease-renewed worker for native read and ledger-backed local effects.

    Actor identity is resolved afresh from a trusted caller-supplied source.
    Admission-time scopes are only a ceiling, never an authentication source.
    """

    def __init__(
        self,
        controller: RunController,
        runner: Runner | None,
        resolve_actor: ActorResolver,
        *,
        worker_id: str,
        cancellation_only: bool = False,
        lease_seconds: int = 30,
        retry_delay_seconds: int = 30,
        next_turn_delay_seconds: int = 1,
    ) -> None:
        if lease_seconds < 3 or retry_delay_seconds < 1 or next_turn_delay_seconds < 1:
            raise ValueError("worker lease and wake-up delays must be positive")
        if not worker_id:
            raise ValueError("worker_id is required")
        if runner is None and not cancellation_only:
            raise ValueError("runner is required unless cancellation_only is enabled")
        self.controller = controller
        self.runner = runner
        self.resolve_actor = resolve_actor
        self.worker_id = worker_id
        self.cancellation_only = cancellation_only
        self.lease_seconds = lease_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self.next_turn_delay_seconds = next_turn_delay_seconds

    def run_once(self) -> WorkerStep | None:
        claimed = self.controller.claim_job(
            self.worker_id, lease_seconds=self.lease_seconds, kind="cancel_run"
        )
        if claimed is None and not self.cancellation_only:
            claimed = self.controller.claim_job(
                self.worker_id, lease_seconds=self.lease_seconds, kind="advance_run"
            )
        if claimed is None:
            return None
        assert claimed.run_id is not None
        stop = Event()
        lease_error: list[Exception] = []

        def heartbeat() -> None:
            while not stop.wait(self.lease_seconds / 3):
                try:
                    self.controller.renew_job_lease(
                        claimed, self.worker_id, lease_seconds=self.lease_seconds
                    )
                except Exception as exc:
                    lease_error.append(exc)
                    stop.set()
                    return

        def finish(disposition: str, delay: int | None) -> WorkerStep:
            stop.set()
            thread.join(timeout=self.lease_seconds / 3 + 1)
            if thread.is_alive():
                raise DependencyUnavailable("job lease heartbeat shutdown")
            if lease_error:
                raise lease_error[0]
            return self._finish(claimed, disposition, delay)

        thread = Thread(target=heartbeat, name="hnh-run-lease", daemon=True)
        thread.start()
        try:
            try:
                tenant_id, subject_id = self.controller.claimed_run_actor(claimed, self.worker_id)
                try:
                    actor = self.resolve_actor(tenant_id, subject_id)
                except HarnessError as exc:
                    return finish(exc.code, self.retry_delay_seconds)
                if actor is None or (actor.tenant_id, actor.subject_id) != (
                    tenant_id,
                    subject_id,
                ):
                    return finish("identity_unavailable", self.retry_delay_seconds)
                if claimed.kind == "cancel_run":
                    try:
                        cancelled = self.controller.finalize_run_cancellation(
                            actor,
                            claimed.run_id,
                            claimed_job=claimed,
                            worker_id=self.worker_id,
                        )
                    except VersionConflict:
                        # This path reads already committed local receipts only.
                        # It must not dispatch another effect while cancelling.
                        try:
                            for action in self.controller.list_actions(actor, claimed.run_id):
                                if action.status == ActionStatus.RUNNING:
                                    self.controller.reconcile_local_action_for_cancellation(
                                        actor,
                                        action.id,
                                        claimed_job=claimed,
                                        worker_id=self.worker_id,
                                    )
                            cancelled = self.controller.finalize_run_cancellation(
                                actor,
                                claimed.run_id,
                                claimed_job=claimed,
                                worker_id=self.worker_id,
                            )
                        except VersionConflict:
                            # Missing or inconsistent receipts, active effects or
                            # descendants remain unresolved, never inferred stopped.
                            return finish(
                                "awaiting_cancellation_evidence", self.retry_delay_seconds
                            )
                    return finish(cancelled.status.value, None)
                assert self.runner is not None
                run = self.runner.advance(
                    actor, claimed.run_id, claimed_job=claimed, worker_id=self.worker_id
                )
            except LeaseLost:
                raise
            except BudgetExhausted as exc:
                run = self.controller.terminate_run(
                    claimed.run_id,
                    code=exc.code,
                    detail=exc.detail or "run budget exhausted",
                    claimed_job=claimed,
                    worker_id=self.worker_id,
                )
            except (ProviderUnavailable, DependencyUnavailable) as exc:
                return finish(exc.code, self.retry_delay_seconds)
            except MigrationRequired as exc:
                self.controller.block_run(
                    claimed.run_id,
                    code=exc.code,
                    detail=exc.title,
                    claimed_job=claimed,
                    worker_id=self.worker_id,
                )
                return finish("blocked", None)
            except HarnessError as exc:
                self.controller.block_run(
                    claimed.run_id,
                    code=exc.code,
                    detail=exc.title,
                    claimed_job=claimed,
                    worker_id=self.worker_id,
                )
                return finish("blocked", None)
            if lease_error:
                raise lease_error[0]
            if run.status == RunStatus.RUNNING:
                return finish("deferred", self.next_turn_delay_seconds)
            return finish(run.status.value, None)
        finally:
            stop.set()
            thread.join(timeout=self.lease_seconds / 3 + 1)

    def _finish(self, claimed: ClaimedJob, disposition: str, delay: int | None) -> WorkerStep:
        if delay is None:
            self.controller.complete_job(claimed, self.worker_id)
        else:
            self.controller.defer_job(claimed, self.worker_id, delay_seconds=delay)
        assert claimed.run_id is not None
        return WorkerStep(claimed.run_id, disposition)
