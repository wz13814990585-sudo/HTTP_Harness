from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class HarnessError(Exception):
    status: int
    code: str
    title: str
    detail: str | None = None
    retry_hint: str = "never"
    extensions: dict[str, Any] = field(default_factory=dict)


class AuthenticationRequired(HarnessError):
    def __init__(self) -> None:
        super().__init__(401, "authentication_required", "Authentication required")


class PermissionDenied(HarnessError):
    def __init__(self) -> None:
        super().__init__(403, "permission_denied", "Permission denied")


class ResourceNotFound(HarnessError):
    def __init__(self, resource: str = "resource") -> None:
        super().__init__(404, "not_found", "Resource not found", f"No visible {resource} exists")


class IdempotencyConflict(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            409,
            "idempotency_conflict",
            "Idempotency key conflict",
            "The key is already bound to a different request fingerprint",
        )


class AlreadyTerminal(HarnessError):
    def __init__(self) -> None:
        super().__init__(409, "already_terminal", "Run is already terminal")


class InvalidTransition(HarnessError):
    def __init__(self, old: str, new: str) -> None:
        super().__init__(
            409,
            "invalid_transition",
            "Invalid state transition",
            f"Run cannot transition from {old} to {new}",
        )


class VersionConflict(HarnessError):
    def __init__(self) -> None:
        super().__init__(409, "version_conflict", "Aggregate version conflict")


class DependencyUnavailable(HarnessError):
    def __init__(self, dependency: str) -> None:
        super().__init__(
            503,
            "dependency_unavailable",
            "Required dependency unavailable",
            f"{dependency} is not configured",
            "after_refresh",
        )


class SchemaInvalid(HarnessError):
    def __init__(self, detail: str) -> None:
        super().__init__(400, "schema_invalid", "Operation schema is invalid", detail)


class InvalidOperation(HarnessError):
    def __init__(self, detail: str) -> None:
        super().__init__(400, "invalid_operation", "Operation is not allowed", detail)


class LeaseDispatchUnsupported(InvalidOperation):
    """A worker lease cannot yet protect this effect adapter's commit boundary."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.status = 409
        self.code = "lease_dispatch_unsupported"
        self.title = "Lease-bound dispatch is unavailable"


class PreconditionRequired(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            428,
            "precondition_required",
            "A write precondition is required",
            "Use If-Match for overwrite or If-None-Match: * for create",
        )


class PreconditionFailed(HarnessError):
    def __init__(self) -> None:
        super().__init__(412, "precondition_failed", "The write precondition did not match")


class PayloadTooLarge(HarnessError):
    def __init__(self) -> None:
        super().__init__(413, "payload_too_large", "Request body is too large")


class UnsupportedMediaType(HarnessError):
    def __init__(self) -> None:
        super().__init__(415, "unsupported_media_type", "Unsupported media type")


class BudgetExhausted(HarnessError):
    def __init__(self, dimension: str) -> None:
        super().__init__(
            409,
            "budget_exhausted",
            "Run budget is exhausted",
            f"No remaining {dimension} budget is available",
        )


class ModelOutputInvalid(HarnessError):
    def __init__(self, detail: str) -> None:
        super().__init__(422, "model_output_invalid", "Model output is invalid", detail)


class ProviderUnavailable(HarnessError):
    def __init__(self, detail: str) -> None:
        super().__init__(503, "provider_unavailable", "Model provider unavailable", detail)


class EventCursorExpired(HarnessError):
    def __init__(self, run_id: str, minimum_after: int) -> None:
        super().__init__(
            410,
            "event_cursor_expired",
            "Event cursor is no longer retained",
            f"Resume from event history at or after {minimum_after}",
            "after_refresh",
        )
        self.extensions = {
            "history_url": f"/v1/runs/{run_id}/event-history?after={minimum_after}",
            "snapshot_url": f"/v1/runs/{run_id}",
            "minimum_after": minimum_after,
        }


class FutureEventCursor(HarnessError):
    def __init__(self, run_id: str, current_sequence: int) -> None:
        super().__init__(
            409,
            "event_cursor_future",
            "Event cursor is ahead of committed history",
            f"Current committed sequence is {current_sequence}",
            "after_refresh",
        )
        self.extensions = {
            "history_url": f"/v1/runs/{run_id}/event-history?after={current_sequence}",
            "snapshot_url": f"/v1/runs/{run_id}",
            "current_sequence": current_sequence,
        }


class ExecutionUnavailable(HarnessError):
    def __init__(self, detail: str) -> None:
        super().__init__(
            503,
            "execution_unavailable",
            "Isolated execution is unavailable",
            detail,
            "after_refresh",
        )


class EnvironmentLost(HarnessError):
    def __init__(self, detail: str = "The execution environment no longer exists") -> None:
        super().__init__(
            409,
            "environment_lost",
            "Execution environment was lost",
            detail,
            "after_reconciliation",
        )


class ExecutionBusy(HarnessError):
    def __init__(self) -> None:
        super().__init__(409, "execution_busy", "Execution session is busy", retry_hint="backoff")


class ApprovalRequired(HarnessError):
    def __init__(self, detail: str = "The exact action does not have a valid approval") -> None:
        super().__init__(
            409,
            "approval_required",
            "Approval is required",
            detail,
            "after_user_input",
        )


class InputRequestExpired(HarnessError):
    def __init__(self) -> None:
        super().__init__(409, "input_request_expired", "Input request has expired")


class LeaseLost(HarnessError):
    def __init__(self) -> None:
        super().__init__(409, "lease_lost", "Worker lease is no longer valid")


class OutcomeUnknown(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            409,
            "outcome_unknown",
            "External outcome is unknown",
            "The operation must be reconciled before it can continue",
            "after_reconciliation",
        )


class MigrationRequired(HarnessError):
    def __init__(self, detail: str) -> None:
        super().__init__(
            409,
            "migration_required",
            "Stored checkpoint is incompatible",
            detail,
            "after_migration",
        )
