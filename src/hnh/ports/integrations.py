from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from hnh.application.operations import BoundOperation
from hnh.domain.identity import TrustedContext


@dataclass(frozen=True, slots=True)
class IntegrationExecution:
    """A trusted adapter result after ActionGateway admission."""

    value: dict[str, Any]
    status_code: int
    succeeded: bool
    terminal: bool = True
    committed: bool = False


class IntegrationExecutor(Protocol):
    def handles(self, capability_id: str) -> bool: ...

    def pending(self, context: TrustedContext, action_id: str) -> IntegrationExecution: ...

    def resume_if_ready(
        self, context: TrustedContext, action_id: str
    ) -> IntegrationExecution | None: ...

    def invoke(
        self,
        context: TrustedContext,
        action_id: str,
        bound: BoundOperation,
        *,
        idempotency_key: str,
    ) -> IntegrationExecution: ...
