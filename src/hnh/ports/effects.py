from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class EffectDispatchResult:
    status_code: int
    body: dict[str, Any] = field(default_factory=dict)
    applied: bool | None = None
    upstream_handle: str | None = None


@dataclass(frozen=True, slots=True)
class EffectReconciliation:
    resolution: str
    evidence_ids: tuple[str, ...]
    comment: str | None = None


@dataclass(frozen=True, slots=True)
class EffectCancellation:
    acknowledged: bool
    stopped: bool
    receipt: dict[str, Any] = field(default_factory=dict)


class AmbiguousDispatch(Exception):
    def __init__(
        self,
        receipt: dict[str, Any],
        *,
        upstream_handle: str | None = None,
    ) -> None:
        super().__init__("external response was lost after possible dispatch")
        self.receipt = receipt
        self.upstream_handle = upstream_handle


class EffectDriver(Protocol):
    def dispatch(
        self,
        action_id: str,
        operation: dict[str, Any],
        downstream_idempotency_key: str | None,
    ) -> EffectDispatchResult: ...

    def reconcile(
        self,
        action_id: str,
        upstream_handle: str | None,
        downstream_idempotency_key: str | None,
    ) -> EffectReconciliation | None: ...

    def cancel(
        self,
        action_id: str,
        upstream_handle: str | None,
    ) -> EffectCancellation: ...
