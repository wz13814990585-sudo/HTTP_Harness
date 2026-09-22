from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hnh.application.run_controller import ActionSnapshot, RunController
from hnh.domain.errors import OutcomeUnknown, VersionConflict
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus
from hnh.ports.effects import AmbiguousDispatch, EffectDriver


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    action: ActionSnapshot
    decision: str


class RecoveryCoordinator:
    """Apply effect-specific retry rules without asking a model to guess outcomes."""

    def __init__(self, controller: RunController) -> None:
        self.controller = controller

    def dispatch(
        self,
        context: TrustedContext,
        action_id: str,
        driver: EffectDriver,
    ) -> RecoveryDecision:
        action = self.controller.get_action(context, action_id)
        downstream_key = (
            f"hnh:{action.id}" if action.effect_semantics == "remote_idempotent" else None
        )
        if action.status == ActionStatus.READY:
            if not self.controller.begin_action_dispatch(
                action.id,
                downstream_idempotency_key=downstream_key,
            ):
                raise VersionConflict()
        elif action.status == ActionStatus.RETRY_WAIT:
            if downstream_key is None:
                raise VersionConflict()
            self.controller.begin_retry_dispatch(
                action.id,
                downstream_idempotency_key=downstream_key,
            )
        else:
            raise VersionConflict()
        try:
            response = driver.dispatch(action.id, action.bound_operation, downstream_key)
        except AmbiguousDispatch as exc:
            ambiguous_receipt = {
                **exc.receipt,
                "transport_ok": False,
                "possible_dispatch": True,
            }
            if action.effect_semantics == "remote_idempotent" and downstream_key is not None:
                current = self.controller.mark_action_retry_wait(
                    action.id,
                    receipt=ambiguous_receipt,
                    downstream_idempotency_key=downstream_key,
                )
                return RecoveryDecision(current, "retry_same_key")
            current = self.controller.mark_action_outcome_unknown(
                action.id,
                receipt=ambiguous_receipt,
                upstream_handle=exc.upstream_handle,
            )
            return RecoveryDecision(current, "reconcile_only")

        receipt: dict[str, Any] = {
            "status_code": response.status_code,
            "body": response.body,
            "applied": response.applied,
            "upstream_handle": response.upstream_handle,
            "transport_ok": True,
        }
        if 200 <= response.status_code < 300:
            current = self.controller.complete_action(
                action.id,
                result=receipt,
                succeeded=True,
                transport_kind="effect_driver",
                response_status=response.status_code,
            )
            return RecoveryDecision(current, "completed")
        if response.applied is False:
            current = self.controller.complete_action(
                action.id,
                result=receipt,
                succeeded=False,
                transport_kind="effect_driver",
                response_status=response.status_code,
            )
            return RecoveryDecision(current, "confirmed_failed")
        if action.effect_semantics == "remote_idempotent" and downstream_key is not None:
            current = self.controller.mark_action_retry_wait(
                action.id,
                receipt=receipt,
                downstream_idempotency_key=downstream_key,
            )
            return RecoveryDecision(current, "retry_same_key")
        current = self.controller.mark_action_outcome_unknown(
            action.id,
            receipt=receipt,
            upstream_handle=response.upstream_handle,
        )
        return RecoveryDecision(current, "reconcile_only")

    def reconcile(
        self,
        context: TrustedContext,
        action_id: str,
        driver: EffectDriver,
        *,
        idempotency_key: str,
    ) -> RecoveryDecision:
        action = self.controller.get_action(context, action_id)
        if action.status != ActionStatus.OUTCOME_UNKNOWN:
            raise VersionConflict()
        evidence = driver.reconcile(
            action.id,
            action.upstream_handle,
            action.downstream_idempotency_key,
        )
        if evidence is None:
            raise OutcomeUnknown()
        current = self.controller.reconcile_action(
            context,
            action.id,
            expected_version=action.version,
            resolution=evidence.resolution,
            evidence_ids=list(evidence.evidence_ids),
            comment=evidence.comment,
            idempotency_key=idempotency_key,
        )
        return RecoveryDecision(current, evidence.resolution)

    def cancel(
        self,
        context: TrustedContext,
        action_id: str,
        driver: EffectDriver,
    ) -> RecoveryDecision:
        action = self.controller.get_action(context, action_id)
        cancellation = driver.cancel(action.id, action.upstream_handle)
        current = self.controller.record_action_cancel_receipt(
            action.id,
            receipt={
                **cancellation.receipt,
                "acknowledged": cancellation.acknowledged,
                "stopped": cancellation.stopped,
            },
            stopped=cancellation.stopped,
        )
        return RecoveryDecision(
            current,
            "stopped" if cancellation.stopped else "awaiting_confirmation",
        )
