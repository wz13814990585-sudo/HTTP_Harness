from __future__ import annotations

import os
from dataclasses import dataclass

from hnh.adapters.decision.typesafe import (
    TYPESAFE_DEFAULT_MODEL,
    TYPESAFE_SYSTEMONE_URL,
    validate_typesafe_configuration,
)
from hnh.adapters.models.deepseek_responses import validate_deepseek_configuration
from hnh.application.auth import DevelopmentAuthenticator, DevelopmentPrincipal


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str | None
    development_token: str | None
    development_tenant: str = "local"
    development_subject: str = "developer"
    deepseek_api_key: str | None = None
    deepseek_model: str = "deepseek-flash"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_reasoning_effort: str = "none"
    typesafe_enabled: bool = False
    typesafe_api_key: str | None = None
    typesafe_model: str = TYPESAFE_DEFAULT_MODEL
    typesafe_base_url: str = TYPESAFE_SYSTEMONE_URL
    typesafe_timeout_seconds: float = 2.0
    typesafe_min_confidence: float = 0.8
    sandbox_image: str | None = None
    sandbox_profile_id: str = "python-safe"
    blob_root: str | None = None
    otlp_traces_endpoint: str | None = None

    def __post_init__(self) -> None:
        validate_deepseek_configuration(
            self.deepseek_model,
            self.deepseek_reasoning_effort,
        )
        validate_typesafe_configuration(
            enabled=self.typesafe_enabled,
            api_key=self.typesafe_api_key,
            model=self.typesafe_model,
            endpoint=self.typesafe_base_url,
            timeout_seconds=self.typesafe_timeout_seconds,
            min_confidence=self.typesafe_min_confidence,
        )

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            database_url=os.environ.get("HNH_DATABASE_URL"),
            development_token=os.environ.get("HNH_DEV_TOKEN"),
            development_tenant=os.environ.get("HNH_DEV_TENANT", "local"),
            development_subject=os.environ.get("HNH_DEV_SUBJECT", "developer"),
            deepseek_api_key=os.environ.get("HNH_DEEPSEEK_API_KEY"),
            deepseek_model=os.environ.get("HNH_DEEPSEEK_MODEL", "deepseek-flash"),
            deepseek_base_url=os.environ.get("HNH_DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            deepseek_reasoning_effort=os.environ.get("HNH_DEEPSEEK_REASONING_EFFORT", "none"),
            typesafe_enabled=_environment_bool("HNH_TYPESAFE_ENABLED", default=False),
            typesafe_api_key=os.environ.get("TYPESAFE_API_KEY"),
            typesafe_model=os.environ.get("HNH_TYPESAFE_MODEL", TYPESAFE_DEFAULT_MODEL),
            typesafe_base_url=os.environ.get("HNH_TYPESAFE_BASE_URL", TYPESAFE_SYSTEMONE_URL),
            typesafe_timeout_seconds=float(os.environ.get("HNH_TYPESAFE_TIMEOUT_SECONDS", "2.0")),
            typesafe_min_confidence=float(os.environ.get("HNH_TYPESAFE_MIN_CONFIDENCE", "0.8")),
            sandbox_image=os.environ.get("HNH_SANDBOX_IMAGE"),
            sandbox_profile_id=os.environ.get("HNH_SANDBOX_PROFILE_ID", "python-safe"),
            blob_root=os.environ.get("HNH_BLOB_ROOT"),
            otlp_traces_endpoint=os.environ.get("HNH_OTLP_TRACES_ENDPOINT"),
        )

    def authenticator(self) -> DevelopmentAuthenticator:
        principals: dict[str, DevelopmentPrincipal] = {}
        if self.development_token:
            principals[self.development_token] = DevelopmentPrincipal(
                tenant_id=self.development_tenant,
                subject_id=self.development_subject,
                scopes=frozenset(
                    {
                        "actions:read",
                        "actions:reconcile",
                        "artifacts:read",
                        "artifacts:write",
                        "capabilities:read",
                        "execution:read",
                        "execution:write",
                        "integrations:invoke",
                        "inputs:read",
                        "inputs:respond",
                        "metrics:read",
                        "runs:read",
                        "runs:write",
                        "workspace:read",
                        "workspace:write",
                    }
                ),
            )
        return DevelopmentAuthenticator(principals)


def _environment_bool(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be one of 1/0, true/false, yes/no, on/off")
