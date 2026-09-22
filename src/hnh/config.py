from __future__ import annotations

import os
from dataclasses import dataclass

from hnh.application.auth import DevelopmentAuthenticator, DevelopmentPrincipal


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str | None
    development_token: str | None
    development_tenant: str = "local"
    development_subject: str = "developer"
    openai_api_key: str | None = None
    openai_model: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    sandbox_image: str | None = None
    sandbox_profile_id: str = "python-safe"
    blob_root: str | None = None
    otlp_traces_endpoint: str | None = None

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            database_url=os.environ.get("HNH_DATABASE_URL"),
            development_token=os.environ.get("HNH_DEV_TOKEN"),
            development_tenant=os.environ.get("HNH_DEV_TENANT", "local"),
            development_subject=os.environ.get("HNH_DEV_SUBJECT", "developer"),
            openai_api_key=os.environ.get("HNH_OPENAI_API_KEY"),
            openai_model=os.environ.get("HNH_OPENAI_MODEL"),
            openai_base_url=os.environ.get("HNH_OPENAI_BASE_URL", "https://api.openai.com/v1"),
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
