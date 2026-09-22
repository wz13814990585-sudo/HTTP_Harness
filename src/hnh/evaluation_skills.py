"""Opt-in skills ablation over durable native echo Runs, not a benchmark result."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import Engine

from hnh.adapters.models.deepseek_responses import (
    DeepSeekResponsesProvider,
    validate_deepseek_configuration,
)
from hnh.adapters.postgres.database import build_engine
from hnh.application.capabilities import CapabilityRegistry
from hnh.application.skills import SkillLoader
from hnh.domain.identity import TrustedContext
from hnh.evaluation import (
    AblationFeature,
    AblationRunner,
    EvaluationCase,
    EvaluationControls,
    EvaluationDesignError,
    EvaluationResult,
    stable_hash,
)
from hnh.evaluation_live import LiveEchoExecutor, live_echo_cases
from hnh.ports.models import ModelProvider

ModelFactory = Callable[[], ModelProvider]
SKILL_ID = "p08-echo-guidance"
AGENT_ID = "p08-live-echo"
SKILL_FILE = "echo_guidance.md"


class LiveSkillsAblationExecutor:
    """One feature flag changes only the trusted allowlisted skill loader."""

    def __init__(
        self,
        engine: Engine,
        context: TrustedContext,
        model_factory: ModelFactory,
        cases: tuple[EvaluationCase, ...],
        skill_root: Path,
        *,
        campaign_id: str,
    ) -> None:
        if not campaign_id or not cases or len({case.case_id for case in cases}) != len(cases):
            raise ValueError("skills ablation requires a campaign and unique cases")
        self.engine = engine
        self.context = context
        self.model_factory = model_factory
        self.cases = cases
        self.campaign_id = campaign_id
        self.skill_loader = SkillLoader(
            skill_root,
            {SKILL_ID: SKILL_FILE},
            {AGENT_ID: (SKILL_ID,)},
        )
        self.skill_ref = self.skill_loader.load(SKILL_ID).source_ref()
        native = CapabilityRegistry().get(context, "native.echo")
        self.capability_revision = f"{native.id}:{native.revision}"
        self.fixtures_hash = stable_hash(
            {"cases": [asdict(case) for case in cases], "skill_ref": self.skill_ref}
        )

    def preflight(self, controls: EvaluationControls) -> None:
        if controls.capability_revision != self.capability_revision:
            raise EvaluationDesignError("skills ablation capability revision differs from controls")
        if controls.fixtures_hash != self.fixtures_hash:
            raise EvaluationDesignError("skills ablation fixtures differ from controls")
        if controls.scopes != tuple(sorted(self.context.scopes)):
            raise EvaluationDesignError("skills ablation scopes differ from controls")
        if controls.policy_revision != "dev-1":
            raise EvaluationDesignError("skills ablation policy revision differs from controls")
        if self.skill_loader.load(SKILL_ID).source_ref() != self.skill_ref:
            raise EvaluationDesignError("skills ablation allowlisted skill changed after pinning")

    def run(
        self,
        case: EvaluationCase,
        controls: EvaluationControls,
        feature: AblationFeature,
        enabled: bool,
    ) -> EvaluationResult:
        self.preflight(controls)
        if feature != "skills" or case not in self.cases or type(enabled) is not bool:
            raise EvaluationDesignError("skills ablation received an unexpected case or feature")
        echo = LiveEchoExecutor(
            self.engine,
            self.context,
            lambda _surface: self.model_factory(),
            campaign_id=f"{self.campaign_id}-skills-{'on' if enabled else 'off'}",
            skill_loader=self.skill_loader if enabled else None,
        )
        return echo.run(case, controls, "http_semantic_operation", "native_http")


@dataclass(frozen=True, slots=True)
class LiveSkillsConfig:
    database_url: str
    api_key: str
    model: str
    base_url: str
    reasoning_effort: str
    output: Path
    tenant_id: str
    subject_id: str
    implementation_revision: str

    @classmethod
    def from_environment(cls) -> LiveSkillsConfig:
        required = (
            "HNH_DATABASE_URL",
            "HNH_DEEPSEEK_API_KEY",
            "HNH_EVAL_SKILLS_OUTPUT",
            "HNH_EVAL_TENANT",
            "HNH_EVAL_SUBJECT",
            "HNH_EVAL_IMPLEMENTATION_REVISION",
        )
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise ValueError(f"live skills ablation configuration missing: {', '.join(missing)}")
        values = {name: os.environ[name] for name in required}
        output = Path(values["HNH_EVAL_SKILLS_OUTPUT"]).resolve()
        if output.exists():
            raise FileExistsError("skills ablation raw JSONL destination already exists")
        revision = values["HNH_EVAL_IMPLEMENTATION_REVISION"]
        if re.fullmatch(r"[A-Za-z0-9._:/@+-]{1,200}", revision) is None:
            raise ValueError("HNH_EVAL_IMPLEMENTATION_REVISION has an invalid format")
        model = os.environ.get("HNH_DEEPSEEK_MODEL", "deepseek-flash")
        reasoning_effort = os.environ.get("HNH_DEEPSEEK_REASONING_EFFORT", "none")
        validate_deepseek_configuration(model, reasoning_effort)
        return cls(
            values["HNH_DATABASE_URL"],
            values["HNH_DEEPSEEK_API_KEY"],
            model,
            os.environ.get("HNH_DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            reasoning_effort,
            output,
            values["HNH_EVAL_TENANT"],
            values["HNH_EVAL_SUBJECT"],
            revision,
        )


def run_live_skills(config: LiveSkillsConfig) -> Path:
    """Run the independent off/on experiment only when explicitly configured."""
    context = TrustedContext(
        config.tenant_id,
        config.subject_id,
        frozenset({"runs:read", "integrations:invoke"}),
    )
    cases = live_echo_cases()
    skill_root = Path(__file__).resolve().parents[2] / "eval" / "fixtures"
    engine = build_engine(config.database_url)
    try:
        executor = LiveSkillsAblationExecutor(
            engine,
            context,
            lambda: DeepSeekResponsesProvider(
                api_key=config.api_key,
                model=config.model,
                base_url=config.base_url,
                reasoning_effort=config.reasoning_effort,
            ),
            cases,
            skill_root,
            campaign_id=uuid4().hex,
        )
        controls = EvaluationControls(
            model=config.model,
            model_settings={
                "max_output_tokens_per_turn": 1024,
                "reasoning_effort": config.reasoning_effort,
                "stream": False,
            },
            capability_revision=executor.capability_revision,
            policy_revision="dev-1",
            scopes=tuple(sorted(context.scopes)),
            budget={"max_model_turns": 3, "max_tool_calls": 1, "max_output_tokens": 2048},
            timeout_seconds=180.0,
            fixtures_hash=executor.fixtures_hash,
            environment_hash=stable_hash(
                {
                    "implementation_revision": config.implementation_revision,
                    "model_adapter": "deepseek-responses",
                    "model_base_url": config.base_url,
                    "reasoning_effort": config.reasoning_effort,
                    "ablation": "skills-v1",
                }
            ),
            evidence_tier="live_provider",
        )
        executor.preflight(controls)
        AblationRunner(executor, config.output).run(cases, controls, "skills")
    finally:
        engine.dispose()
    return config.output
