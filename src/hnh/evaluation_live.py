"""Opt-in real-model four-arm echo evaluation; never used by the API worker."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from hnh.adapters.mcp.oauth import InMemoryOAuthTokenStorage, IssuerPinnedOAuthMCPTransport
from hnh.adapters.mcp.sdk import OfficialSDKMCPClient
from hnh.adapters.models.deepseek_responses import (
    DeepSeekFunctionToolsProvider,
    DeepSeekResponsesProvider,
    validate_deepseek_configuration,
)
from hnh.adapters.postgres.database import build_engine
from hnh.adapters.postgres.models import EventRecord, ModelCallRecord
from hnh.application.action_gateway import ActionGateway
from hnh.application.capabilities import Capability, CapabilityRegistry
from hnh.application.completion import CompletionGate
from hnh.application.context import ContextBuilder
from hnh.application.credentials import InMemoryOAuthClientVault, OAuthClientCredentialBinding
from hnh.application.mcp_catalog import MCPIntegrationBinding
from hnh.application.mcp_composition import MCPIntegrationManager
from hnh.application.operations import BoundOperation
from hnh.application.resources import ResourceStore
from hnh.application.run_controller import ActionSnapshot, RunController
from hnh.application.runner import Runner
from hnh.application.skills import SkillLoader
from hnh.domain.errors import HarnessError
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus, RunStatus
from hnh.evaluation import (
    Downstream,
    EvaluationCase,
    EvaluationControls,
    EvaluationDesignError,
    EvaluationResult,
    ExperimentRunner,
    ModelSurface,
    Outcome,
    stable_hash,
)
from hnh.ports.integrations import IntegrationExecution, IntegrationExecutor
from hnh.ports.models import ModelProvider

ModelFactory = Callable[[ModelSurface], ModelProvider]


class _MCPAliasExecutor:
    """Route the canonical model-visible echo to one pinned MCP capability."""

    def __init__(
        self,
        delegate: IntegrationExecutor,
        public_capability_id: str,
        remote_capability: Capability,
    ) -> None:
        self._delegate = delegate
        self._public_capability_id = public_capability_id
        self._remote_capability = remote_capability

    def handles(self, capability_id: str) -> bool:
        return capability_id == self._public_capability_id

    def pending(self, context: TrustedContext, action_id: str) -> IntegrationExecution:
        return self._delegate.pending(context, action_id)

    def resume_if_ready(
        self, context: TrustedContext, action_id: str
    ) -> IntegrationExecution | None:
        return self._delegate.resume_if_ready(context, action_id)

    def invoke(
        self,
        context: TrustedContext,
        action_id: str,
        bound: BoundOperation,
        *,
        idempotency_key: str,
    ) -> IntegrationExecution:
        remote_bound = replace(bound, capability=self._remote_capability)
        return self._delegate.invoke(
            context,
            action_id,
            remote_bound,
            idempotency_key=idempotency_key,
        )


class _MCPAliasActionGateway(ActionGateway):
    """Prefer the trusted MCP binding when it shadows the canonical echo ID."""

    def execute_bound(
        self,
        context: TrustedContext,
        action_id: str,
        bound: BoundOperation,
        *,
        idempotency_key: str,
    ) -> tuple[dict[str, object], int, bool] | IntegrationExecution:
        if self.integration_executor is not None and self.integration_executor.handles(
            bound.capability.id
        ):
            return self.integration_executor.invoke(
                context,
                action_id,
                bound,
                idempotency_key=idempotency_key,
            )
        return super().execute_bound(
            context,
            action_id,
            bound,
            idempotency_key=idempotency_key,
        )


def live_echo_cases() -> tuple[EvaluationCase, ...]:
    """Frozen read-only fixture set; diverse inputs, still only one tool family."""
    messages = (
        "same-input",
        "report-ready-42",
        "alpha, beta; gamma",
        "数据-ready-7",
    )
    return tuple(
        EvaluationCase(
            f"echo-{index:03d}",
            f"Use the one authorized echo capability exactly once with message '{message}'. "
            "Then finish using the committed action ID as evidence; do not invent evidence.",
            (f"echo:{message}",),
        )
        for index, message in enumerate(messages, start=1)
    )


class LiveEchoExecutor:
    """One synchronous, read-only Run per arm with an exact committed-action oracle."""

    def __init__(
        self,
        engine: Engine,
        context: TrustedContext,
        model_factory: ModelFactory,
        mcp_manager: MCPIntegrationManager | None = None,
        *,
        campaign_id: str,
        skill_loader: SkillLoader | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        if not campaign_id:
            raise ValueError("campaign_id is required")
        self.engine = engine
        self.context = context
        self.model_factory = model_factory
        self.mcp_manager = mcp_manager
        self.campaign_id = campaign_id
        self.skill_loader = skill_loader
        self._clock = monotonic_clock or time.monotonic
        self._expected_remote_revision: str | None = None

    def preflight(self) -> str:
        """Pin a comparable remote echo contract before opening a raw campaign."""
        if self.mcp_manager is None:
            raise EvaluationDesignError("MCP integration is required for four-arm preflight")
        native = CapabilityRegistry().get(self.context, "native.echo")
        imported, _coordinator = self.mcp_manager.for_context(
            self.context, RunController(self.engine)
        )
        remote = imported.get(self.context, "mcp.eval.echo")
        self._validate_remote_echo(native, remote)
        self._expected_remote_revision = remote.revision
        return f"native.echo:{native.revision}+mcp.eval.echo:{remote.revision}"

    @staticmethod
    def _validate_remote_echo(native: Capability, remote: Capability) -> None:
        if (
            remote.effect_semantics != "read_only"
            or remote.requires_approval
            or remote.required_scope != native.required_scope
            or remote.method != "POST"
            or "application/json" not in remote.request_media_types
            or "complete" not in remote.supported_result_modes
        ):
            raise EvaluationDesignError(
                "evaluation MCP echo is not a comparable read-only capability"
            )
        schema = remote.input_schema
        properties = schema.get("properties")
        if (
            schema.get("type") != "object"
            or not isinstance(properties, dict)
            or set(properties) != {"message"}
            or schema.get("required") != ["message"]
            or not isinstance(properties["message"], dict)
            or properties["message"].get("type") != "string"
        ):
            raise EvaluationDesignError(
                "evaluation MCP echo must accept only a required string message"
            )
        message_schema = properties["message"]
        if set(schema) - {
            "$schema",
            "type",
            "properties",
            "required",
            "additionalProperties",
            "title",
            "description",
        } or set(message_schema) - {"type", "minLength", "maxLength", "title", "description"}:
            raise EvaluationDesignError(
                "evaluation MCP echo has unrecognized constraints on the canonical input"
            )
        min_length = message_schema.get("minLength", 0)
        max_length = message_schema.get("maxLength", 100)
        if (
            type(min_length) is not int
            or min_length > 1
            or type(max_length) is not int
            or max_length < 100
        ):
            raise EvaluationDesignError(
                "evaluation MCP echo input is narrower than the canonical message domain"
            )
        try:
            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(schema)
        except SchemaError as exc:
            raise EvaluationDesignError("evaluation MCP echo input schema is invalid") from exc
        for case in live_echo_cases():
            message = case.expected_evidence[0].removeprefix("echo:")
            if not validator.is_valid({"message": message}):
                raise EvaluationDesignError("evaluation MCP echo rejects a frozen fixture message")

        output_schema = remote.output_schema
        output_properties = output_schema.get("properties")
        output_required = output_schema.get("required")
        if (
            output_schema.get("type") != "object"
            or not isinstance(output_properties, dict)
            or not isinstance(output_required, list)
            or "message" not in output_required
            or not isinstance(output_properties.get("message"), dict)
            or output_properties["message"].get("type") != "string"
        ):
            raise EvaluationDesignError(
                "evaluation MCP echo must declare a required string message output"
            )
        output_message = output_properties["message"]
        if set(output_schema) - {
            "$schema",
            "type",
            "properties",
            "required",
            "additionalProperties",
            "title",
            "description",
        } or set(output_message) - {"type", "minLength", "maxLength", "title", "description"}:
            raise EvaluationDesignError(
                "evaluation MCP echo has unrecognized constraints on the canonical output"
            )
        output_min = output_message.get("minLength", 0)
        output_max = output_message.get("maxLength", 100)
        if (
            type(output_min) is not int
            or output_min > 1
            or type(output_max) is not int
            or output_max < 100
        ):
            raise EvaluationDesignError(
                "evaluation MCP echo output is narrower than the canonical message domain"
            )
        try:
            Draft202012Validator.check_schema(output_schema)
        except SchemaError as exc:
            raise EvaluationDesignError("evaluation MCP echo output schema is invalid") from exc

    @staticmethod
    def _project_echo_result(
        action: ActionSnapshot, result: dict[str, object]
    ) -> dict[str, object]:
        if action.status != ActionStatus.SUCCEEDED or action.capability_id != "native.echo":
            return result
        value = result.get("value")
        if isinstance(value, dict) and isinstance(value.get("structured_content"), dict):
            value = value["structured_content"]
        if isinstance(value, dict) and isinstance(value.get("message"), str):
            return {"status_code": 200, "value": {"message": value["message"]}}
        return result

    def run(
        self,
        case: EvaluationCase,
        controls: EvaluationControls,
        surface: ModelSurface,
        downstream: Downstream,
    ) -> EvaluationResult:
        controller = RunController(self.engine)
        native = CapabilityRegistry().get(self.context, "native.echo")
        if self._expected_remote_revision is not None:
            pinned_revision = (
                f"native.echo:{native.revision}+mcp.eval.echo:{self._expected_remote_revision}"
            )
            if controls.capability_revision != pinned_revision:
                raise EvaluationDesignError(
                    "evaluation controls differ from the preflight capability revision"
                )
        evaluation_timeout = max(1, min(30, int(controls.timeout_seconds)))
        if downstream == "native_http":
            registry = CapabilityRegistry((replace(native, timeout_seconds=evaluation_timeout),))
            integration_executor = None
            capability_id = "native.echo"
            gateway_type = ActionGateway
        else:
            if self.mcp_manager is None:
                raise EvaluationDesignError("MCP integration is unavailable")
            imported, coordinator = self.mcp_manager.for_context(self.context, controller)
            capability_id = "mcp.eval.echo"
            remote = imported.get(self.context, capability_id)
            self._validate_remote_echo(native, remote)
            if (
                self._expected_remote_revision is not None
                and remote.revision != self._expected_remote_revision
            ):
                raise EvaluationDesignError(
                    "evaluation MCP echo capability changed after preflight"
                )
            registry = CapabilityRegistry((replace(native, timeout_seconds=evaluation_timeout),))
            integration_executor = _MCPAliasExecutor(
                coordinator,
                native.id,
                remote,
            )
            capability_id = "native.echo"
            gateway_type = _MCPAliasActionGateway

        provider = self.model_factory(surface)
        if provider.model_revision != controls.model:
            raise ValueError("provider model differs from recorded evaluation control")
        gateway = gateway_type(
            self.engine,
            registry,
            ResourceStore(self.engine),
            controller,
            integration_executor=integration_executor,
        )
        max_output = controls.budget.get("max_output_tokens", 2048)
        runner = Runner(
            controller,
            ContextBuilder(
                controller,
                registry,
                skill_loader=self.skill_loader,
                observation_projector=self._project_echo_result,
            ),
            gateway,
            CompletionGate(self.engine, controller),
            provider,
            default_max_output_tokens=min(1024, max_output),
        )
        admitted = controller.admit_run(
            self.context,
            {
                "agent_id": "p08-live-echo",
                "input": case.task,
                "limits": {
                    "max_model_turns": controls.budget.get("max_model_turns", 3),
                    "max_tool_calls": controls.budget.get("max_tool_calls", 1),
                    "max_output_tokens": max_output,
                    "max_wall_seconds": max(1, int(controls.timeout_seconds)),
                },
            },
            f"eval-{self.campaign_id}-{case.case_id}-{surface}-{downstream}",
        )
        if admitted.replayed:
            raise ValueError("evaluation campaign already admitted this arm")
        run_id = admitted.resource_id
        deadline = self._clock() + controls.timeout_seconds
        failure_reason: str | None = None
        timed_out = False
        for _ in range(5):
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
            # This evaluation exposes only a read-only echo. A normal worker must
            # not use this shortcut for an arbitrary Run with unresolved effects.
            snapshot = controller.fail_run(run_id, code="evaluation_stopped", detail=failure_reason)
        actions = controller.list_actions(self.context, run_id)
        evidence: list[str] = []
        for action in actions:
            if action.capability_id != capability_id or action.status != ActionStatus.SUCCEEDED:
                continue
            value = (action.result or {}).get("value", {})
            if downstream == "mcp_adapter" and isinstance(value, dict):
                value = value.get("structured_content", {})
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                evidence.append(f"echo:{value['message']}")
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
        elif snapshot.status in {RunStatus.BLOCKED, RunStatus.WAITING_INPUT}:
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
class LiveEchoConfig:
    database_url: str
    api_key: str
    model: str
    base_url: str
    reasoning_effort: str
    mcp_endpoint: str
    mcp_issuer: str
    mcp_resource: str
    mcp_client_id: str
    mcp_client_secret: str
    output: Path
    tenant_id: str
    subject_id: str
    implementation_revision: str

    @classmethod
    def from_environment(cls) -> LiveEchoConfig:
        required = (
            "HNH_DATABASE_URL",
            "HNH_DEEPSEEK_API_KEY",
            "HNH_MCP_ENDPOINT",
            "HNH_MCP_ISSUER",
            "HNH_MCP_RESOURCE",
            "HNH_MCP_CLIENT_ID",
            "HNH_MCP_CLIENT_SECRET",
            "HNH_EVAL_OUTPUT",
            "HNH_EVAL_TENANT",
            "HNH_EVAL_SUBJECT",
            "HNH_EVAL_IMPLEMENTATION_REVISION",
        )
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise ValueError(f"live evaluation configuration missing: {', '.join(missing)}")
        values = {name: os.environ[name] for name in required}
        for name in ("HNH_MCP_ENDPOINT", "HNH_MCP_ISSUER", "HNH_MCP_RESOURCE"):
            parsed = urlsplit(values[name])
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(f"{name} must be a credential-free HTTPS URL")
        output = Path(values["HNH_EVAL_OUTPUT"]).resolve()
        if output.exists():
            raise FileExistsError("live raw JSONL destination already exists")
        revision = values["HNH_EVAL_IMPLEMENTATION_REVISION"]
        if re.fullmatch(r"[A-Za-z0-9._:/@+-]{1,200}", revision) is None:
            raise ValueError("HNH_EVAL_IMPLEMENTATION_REVISION has an invalid format")
        model = os.environ.get("HNH_DEEPSEEK_MODEL", "deepseek-flash")
        reasoning_effort = os.environ.get("HNH_DEEPSEEK_REASONING_EFFORT", "high")
        validate_deepseek_configuration(model, reasoning_effort)
        return cls(
            database_url=values["HNH_DATABASE_URL"],
            api_key=values["HNH_DEEPSEEK_API_KEY"],
            model=model,
            base_url=os.environ.get("HNH_DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            reasoning_effort=reasoning_effort,
            mcp_endpoint=values["HNH_MCP_ENDPOINT"],
            mcp_issuer=values["HNH_MCP_ISSUER"],
            mcp_resource=values["HNH_MCP_RESOURCE"],
            mcp_client_id=values["HNH_MCP_CLIENT_ID"],
            mcp_client_secret=values["HNH_MCP_CLIENT_SECRET"],
            output=output,
            tenant_id=values["HNH_EVAL_TENANT"],
            subject_id=values["HNH_EVAL_SUBJECT"],
            implementation_revision=revision,
        )


def run_live_echo(config: LiveEchoConfig) -> Path:
    """Operator-invoked evaluation; requires migrated, dedicated PostgreSQL."""
    context = TrustedContext(
        config.tenant_id,
        config.subject_id,
        frozenset({"runs:read", "integrations:invoke"}),
    )
    binding = MCPIntegrationBinding(
        integration_id="eval",
        profile="modern2026-07-28",
        credential_reference="p08-evaluation",
        issuer=config.mcp_issuer,
        resource=config.mcp_resource,
        tool_effects={"echo": "read_only"},
    )
    vault = InMemoryOAuthClientVault(
        (
            OAuthClientCredentialBinding(
                "p08-evaluation",
                "eval",
                context.tenant_id,
                config.mcp_issuer,
                config.mcp_resource,
                context.subject_id,
                config.mcp_client_id,
                config.mcp_client_secret,
            ),
        )
    )

    def mcp_client(item: MCPIntegrationBinding, actor: TrustedContext) -> OfficialSDKMCPClient:
        transport = IssuerPinnedOAuthMCPTransport(item, actor, vault, InMemoryOAuthTokenStorage())
        return OfficialSDKMCPClient(
            config.mcp_endpoint,
            item,
            subject_id=actor.subject_id,
            tenant_id=actor.tenant_id,
            oauth_transport_factory=transport,
        )

    def model_provider(surface: ModelSurface) -> ModelProvider:
        provider_type = (
            DeepSeekFunctionToolsProvider
            if surface == "native_function_schema"
            else DeepSeekResponsesProvider
        )
        return provider_type(
            api_key=config.api_key,
            model=config.model,
            base_url=config.base_url,
            reasoning_effort=config.reasoning_effort,
        )

    cases = live_echo_cases()
    engine = build_engine(config.database_url)
    try:
        executor = LiveEchoExecutor(
            engine,
            context,
            model_provider,
            MCPIntegrationManager((binding,), mcp_client),
            campaign_id=uuid4().hex,
        )
        revision = executor.preflight()
        controls = EvaluationControls(
            model=config.model,
            model_settings={
                "max_output_tokens_per_turn": 1024,
                "reasoning_effort": config.reasoning_effort,
                "stream": False,
            },
            capability_revision=revision,
            policy_revision=binding.policy_revision,
            scopes=tuple(sorted(context.scopes)),
            budget={"max_model_turns": 3, "max_tool_calls": 1, "max_output_tokens": 2048},
            timeout_seconds=180.0,
            fixtures_hash=stable_hash([asdict(case) for case in cases]),
            environment_hash=stable_hash(
                {
                    "implementation_revision": config.implementation_revision,
                    "model_adapter": "deepseek-responses",
                    "model_base_url": config.base_url,
                    "reasoning_effort": config.reasoning_effort,
                    "mcp_endpoint": config.mcp_endpoint,
                    "mcp_issuer": config.mcp_issuer,
                    "mcp_resource": config.mcp_resource,
                    "mcp_profile": binding.profile,
                }
            ),
            evidence_tier="live_provider",
        )
        ExperimentRunner(executor, config.output).run(cases, controls)
    finally:
        engine.dispose()
    return config.output
