from __future__ import annotations

from enum import StrEnum

from hnh.domain.errors import InvalidTransition


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    BLOCKED = "blocked"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELLING, RunStatus.FAILED, RunStatus.EXPIRED}
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_INPUT,
            RunStatus.BLOCKED,
            RunStatus.CANCELLING,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.EXPIRED,
        }
    ),
    RunStatus.WAITING_INPUT: frozenset(
        {
            RunStatus.QUEUED,
            RunStatus.BLOCKED,
            RunStatus.CANCELLING,
            RunStatus.FAILED,
            RunStatus.EXPIRED,
        }
    ),
    RunStatus.BLOCKED: frozenset(
        {RunStatus.QUEUED, RunStatus.CANCELLING, RunStatus.FAILED, RunStatus.EXPIRED}
    ),
    RunStatus.CANCELLING: frozenset({RunStatus.CANCELLED, RunStatus.BLOCKED, RunStatus.FAILED}),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.EXPIRED: frozenset(),
}

TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.EXPIRED}
)


def require_run_transition(old: RunStatus, new: RunStatus) -> None:
    if new not in RUN_TRANSITIONS[old]:
        raise InvalidTransition(old, new)


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    WAITING_INPUT = "waiting_input"
    READY = "ready"
    RUNNING = "running"
    WAITING_EXTERNAL = "waiting_external"
    RETRY_WAIT = "retry_wait"
    OUTCOME_UNKNOWN = "outcome_unknown"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
