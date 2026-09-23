from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from hnh.application.action_gateway import ActionGateway
from hnh.application.completion import CompletionGate
from hnh.application.context import ContextBuilder
from hnh.application.decision import DecisionRouter
from hnh.application.run_controller import ClaimedJob, ModelCallSnapshot, RunController, RunSnapshot
from hnh.domain.errors import (
    BudgetExhausted,
    HarnessError,
    LeaseDispatchUnsupported,
    LeaseLost,
    ModelOutputInvalid,
)
from hnh.domain.identity import TrustedContext
from hnh.domain.states import TERMINAL_RUN_STATUSES, RunStatus
from hnh.ports.models import ModelLimits, ModelProvider, ModelTurn

FaultHook = Callable[[], None]


def _safe_validation_detail(exc: ValidationError) -> str:
    """Describe repairable schema errors without copying model-controlled values."""
    issues = [
        {
            "type": error["type"],
            "loc": [str(item) for item in error["loc"]],
            "message": error["msg"],
        }
        for error in exc.errors(include_url=False, include_context=False, include_input=False)[:8]
    ]
    return json.dumps(
        {"kind": "model_turn_schema_validation", "issues": issues},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class RunnerHooks:
    after_provider_before_persist: FaultHook | None = None
    after_persist_before_actions: FaultHook | None = None


class Runner:
    """Small persisted model-operation loop; policy/state authority stays outside it."""

    def __init__(
        self,
        controller: RunController,
        context_builder: ContextBuilder,
        gateway: ActionGateway,
        completion_gate: CompletionGate,
        provider: ModelProvider,
        *,
        max_format_repairs: int = 2,
        max_no_progress: int = 2,
        default_max_output_tokens: int = 4096,
        decision_router: DecisionRouter | None = None,
    ) -> None:
        self.controller = controller
        self.context_builder = context_builder
        self.gateway = gateway
        self.completion_gate = completion_gate
        self.provider = provider
        self.max_format_repairs = max_format_repairs
        self.max_no_progress = max_no_progress
        self.default_max_output_tokens = default_max_output_tokens
        self.decision_router = decision_router or DecisionRouter()

    def advance(
        self,
        context: TrustedContext,
        run_id: str,
        *,
        hooks: RunnerHooks | None = None,
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> RunSnapshot:
        if (claimed_job is None) != (worker_id is None):
            raise ValueError("claimed_job and worker_id must be supplied together")
        active_hooks = hooks or RunnerHooks()
        run = self.controller.get_run(context, run_id)
        context = self.controller.effective_context(context, run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            return run
        if run.status == RunStatus.QUEUED:
            run = self.controller.ensure_run_running(
                run_id,
                actor_ref="runner",
                claimed_job=claimed_job,
                worker_id=worker_id,
            )
        if run.status != RunStatus.RUNNING:
            return run

        latest = self.controller.latest_model_call(context, run_id)
        if latest is not None and latest.status == "response_committed":
            self._settle_model_usage(run_id, latest, claimed_job, worker_id)
            return self._process_committed(context, latest, claimed_job, worker_id)

        if latest is not None and latest.status == "started":
            turn_id = latest.turn_id
            max_output_tokens = int(
                latest.request_payload.get("max_output_tokens", self.default_max_output_tokens)
            )
        else:
            turn_id = self.controller.next_turn_id(context, run_id)
            max_output_tokens = self._available_output_budget(context, run_id)
        decision = self.decision_router.decide(run.goal)
        snapshot = self.context_builder.build(
            context,
            run_id,
            turn_id,
            strategy_hint=decision.strategy_hint,
            claimed_job=claimed_job,
            worker_id=worker_id,
        )
        tools = [item.public() for item in self.gateway.registry.list(context, None, 100).items]
        request_payload = {
            "context_snapshot_id": snapshot.id,
            "context_hash": snapshot.content_hash,
            "catalog_revision": self.gateway.registry.catalog_revision,
            "max_output_tokens": max_output_tokens,
            "decision": decision.public(),
        }
        model_reservation = self.controller.reserve_budget(
            run_id,
            reservation_key=f"model:{turn_id}:turn",
            dimension="model_turn",
            amount=1,
            claimed_job=claimed_job,
            worker_id=worker_id,
        )
        try:
            self.controller.reserve_budget(
                run_id,
                reservation_key=f"model:{turn_id}:output",
                dimension="output_tokens",
                amount=max_output_tokens,
                claimed_job=claimed_job,
                worker_id=worker_id,
            )
        except Exception:
            self.controller.release_budget(
                model_reservation.id, claimed_job=claimed_job, worker_id=worker_id
            )
            raise
        work = self.controller.begin_model_call(
            context,
            run_id,
            turn_id=turn_id,
            provider=self.provider.name,
            model_revision=self.provider.model_revision,
            request_payload=request_payload,
            claimed_job=claimed_job,
            worker_id=worker_id,
        )
        if work.replayed:
            if work.call.status == "response_committed":
                self._settle_model_usage(run_id, work.call, claimed_job, worker_id)
                return self._process_committed(context, work.call, claimed_job, worker_id)
            raise ModelOutputInvalid("model turn slot is already closed")
        if work.attempt_id is None:
            raise RuntimeError("new model call has no attempt")
        try:
            response = self.provider.generate(
                snapshot,
                tools,
                ModelLimits(max_output_tokens=max_output_tokens),
            )
            if active_hooks.after_provider_before_persist is not None:
                active_hooks.after_provider_before_persist()
            committed = self.controller.complete_model_call(
                work.call.id,
                work.attempt_id,
                response_payload=response.raw_response,
                parsed_output=response.output,
                provider_request_id=response.provider_request_id,
                usage=response.usage,
                claimed_job=claimed_job,
                worker_id=worker_id,
            )
        except LeaseLost:
            raise
        except HarnessError as exc:
            self.controller.fail_model_call_attempt(
                work.call.id,
                work.attempt_id,
                {"code": exc.code, "detail": exc.detail},
                claimed_job=claimed_job,
                worker_id=worker_id,
            )
            raise
        if active_hooks.after_persist_before_actions is not None:
            active_hooks.after_persist_before_actions()
        self._settle_model_usage(run_id, committed, claimed_job, worker_id)
        return self._process_committed(context, committed, claimed_job, worker_id)

    def run_to_terminal(
        self,
        context: TrustedContext,
        run_id: str,
        *,
        max_steps: int = 50,
    ) -> RunSnapshot:
        for _ in range(max_steps):
            try:
                snapshot = self.advance(context, run_id)
            except BudgetExhausted as exc:
                return self.controller.terminate_run(
                    run_id,
                    code=exc.code,
                    detail=exc.detail or "run budget exhausted",
                )
            if snapshot.status in TERMINAL_RUN_STATUSES:
                return snapshot
        return self.controller.fail_run(
            run_id,
            code="no_progress",
            detail=f"Runner exceeded {max_steps} advancement steps",
        )

    def _process_committed(
        self,
        context: TrustedContext,
        call: ModelCallSnapshot,
        claimed_job: ClaimedJob | None,
        worker_id: str | None,
    ) -> RunSnapshot:
        current = self.controller.get_run(context, call.run_id)
        if current.status != RunStatus.RUNNING:
            # A complete response may arrive after a cancellation/other stop.
            # Keep its committed record, but do not admit another Action.
            return current
        try:
            turn = ModelTurn.model_validate(call.parsed_output)
        except ValidationError as exc:
            return self._reject_model_output(
                context,
                call,
                _safe_validation_detail(exc),
                claimed_job,
                worker_id,
            )
        if (
            call.response_hash is not None
            and self.controller.count_model_calls(
                context,
                call.run_id,
                response_hash=call.response_hash,
            )
            >= self.max_no_progress
        ):
            self.controller.mark_model_call(
                call.id,
                "invalid",
                detail="repeated response made no progress",
                claimed_job=claimed_job,
                worker_id=worker_id,
            )
            return self.controller.fail_run(
                call.run_id,
                code="no_progress",
                detail="model repeated the same complete response without new evidence",
                claimed_job=claimed_job,
                worker_id=worker_id,
            )
        if turn.request_input is not None:
            self.controller.request_clarification(
                context,
                call.run_id,
                prompt=turn.request_input,
                request_hash=call.response_hash or hashlib.sha256(call.id.encode()).hexdigest(),
                requested_schema={"type": "object", "additionalProperties": True},
                expires_at=datetime.now(UTC) + timedelta(days=7),
                claimed_job=claimed_job,
                worker_id=worker_id,
            )
            self.controller.mark_model_call(
                call.id, "processed", claimed_job=claimed_job, worker_id=worker_id
            )
            return self.controller.get_run(context, call.run_id)
        if turn.operations:
            try:
                for operation in turn.operations:
                    self.gateway.bind(context, operation)
            except HarnessError as exc:
                return self._reject_model_output(
                    context,
                    call,
                    f"{exc.code}: {exc.detail or exc.title}",
                    claimed_job,
                    worker_id,
                )
            for index, operation in enumerate(turn.operations):
                reservation = self.controller.reserve_budget(
                    call.run_id,
                    reservation_key=f"model:{call.id}:tool:{index}",
                    dimension="tool_call",
                    amount=1,
                    claimed_job=claimed_job,
                    worker_id=worker_id,
                )
                try:
                    try:
                        self.gateway.execute(
                            context,
                            operation,
                            idempotency_key=f"model-{call.id}-{index}",
                            transport_kind="inproc",
                            run_id=call.run_id,
                            turn_id=call.turn_id,
                            call_index=index,
                            claimed_job=claimed_job,
                            worker_id=worker_id,
                        )
                    except LeaseLost:
                        raise
                    except LeaseDispatchUnsupported:
                        # This failure happened before Action admission. A
                        # normal Action observation cannot be synthesized.
                        raise
                    except HarnessError:
                        # ActionGateway has already committed the structured failure.
                        # The next model turn may recover using that observation.
                        pass
                finally:
                    self.controller.settle_budget(
                        reservation.id,
                        actual_amount=1,
                        claimed_job=claimed_job,
                        worker_id=worker_id,
                    )
            self.controller.mark_model_call(
                call.id, "processed", claimed_job=claimed_job, worker_id=worker_id
            )
            return self.controller.get_run(context, call.run_id)
        if turn.final_candidate is None:
            return self._reject_model_output(
                context, call, "turn has no final candidate", claimed_job, worker_id
            )
        gate = self.completion_gate.verify(
            context,
            call.run_id,
            turn.final_candidate,
            model_call_id=call.id,
            claimed_job=claimed_job,
            worker_id=worker_id,
        )
        self.controller.mark_model_call(
            call.id,
            "processed",
            detail=None if gate.verified else ", ".join(gate.missing_evidence),
            claimed_job=claimed_job,
            worker_id=worker_id,
        )
        return self.controller.get_run(context, call.run_id)

    def _reject_model_output(
        self,
        context: TrustedContext,
        call: ModelCallSnapshot,
        detail: str,
        claimed_job: ClaimedJob | None,
        worker_id: str | None,
    ) -> RunSnapshot:
        self.controller.mark_model_call(
            call.id,
            "invalid",
            detail=detail,
            claimed_job=claimed_job,
            worker_id=worker_id,
        )
        invalid_count = self.controller.count_model_calls(context, call.run_id, status="invalid")
        if invalid_count >= self.max_format_repairs:
            return self.controller.fail_run(
                call.run_id,
                code="model_output_invalid",
                detail=f"model output remained invalid after {invalid_count} attempts",
                claimed_job=claimed_job,
                worker_id=worker_id,
            )
        return self.controller.get_run(context, call.run_id)

    def _settle_model_usage(
        self,
        run_id: str,
        call: ModelCallSnapshot,
        claimed_job: ClaimedJob | None,
        worker_id: str | None,
    ) -> None:
        turn = self.controller.reserve_budget(
            run_id,
            reservation_key=f"model:{call.turn_id}:turn",
            dimension="model_turn",
            amount=1,
            claimed_job=claimed_job,
            worker_id=worker_id,
        )
        max_output = int(
            call.request_payload.get("max_output_tokens", self.default_max_output_tokens)
        )
        output = self.controller.reserve_budget(
            run_id,
            reservation_key=f"model:{call.turn_id}:output",
            dimension="output_tokens",
            amount=max_output,
            claimed_job=claimed_job,
            worker_id=worker_id,
        )
        self.controller.settle_budget(
            turn.id, actual_amount=1, claimed_job=claimed_job, worker_id=worker_id
        )
        actual_output = int(call.usage.get("output_tokens", 0) or 0)
        self.controller.settle_budget(
            output.id,
            actual_amount=actual_output,
            claimed_job=claimed_job,
            worker_id=worker_id,
        )

    def _available_output_budget(self, context: TrustedContext, run_id: str) -> int:
        status = self.controller.budget_status(context, run_id)
        limit = status["limits"]["output_tokens"]
        if limit is None:
            return self.default_max_output_tokens
        remaining = (
            int(limit)
            - int(status["used"]["output_tokens"])
            - int(status["reserved"].get("output_tokens", 0))
        )
        if remaining <= 0:
            raise BudgetExhausted("output_tokens")
        return min(self.default_max_output_tokens, remaining)
