from __future__ import annotations

from typing import Any

from hnh.application.action_gateway import ActionGateway, ExecutionResult
from hnh.application.operations import HttpOperation
from hnh.application.resources import ArtifactSnapshot, FileContent, ResourceStore
from hnh.domain.identity import TrustedContext


class ResourceDispatcher:
    """One transport-neutral entry point for native HTTP resources."""

    def __init__(self, gateway: ActionGateway, resources: ResourceStore) -> None:
        self.gateway = gateway
        self.resources = resources

    def execute_effect(
        self,
        context: TrustedContext,
        operation: HttpOperation,
        *,
        idempotency_key: str,
        transport_kind: str,
    ) -> ExecutionResult:
        return self.gateway.execute(
            context,
            operation,
            idempotency_key=idempotency_key,
            transport_kind=transport_kind,
        )

    def read_workspace_file(
        self,
        context: TrustedContext,
        workspace_id: str,
        file_path: str,
    ) -> FileContent:
        self.gateway.bind(
            context,
            HttpOperation(
                method="GET",
                target=f"/v1/workspaces/{workspace_id}/files/{file_path}",
            ),
        )
        return self.resources.read_file(context, workspace_id, file_path)

    def get_artifact(self, context: TrustedContext, artifact_id: str) -> ArtifactSnapshot:
        self.gateway.bind(
            context,
            HttpOperation(method="GET", target=f"/v1/artifacts/{artifact_id}"),
        )
        return self.resources.get_artifact(context, artifact_id)

    def get_artifact_content(
        self,
        context: TrustedContext,
        artifact_id: str,
    ) -> tuple[bytes, str, str]:
        self.gateway.bind(
            context,
            HttpOperation(method="GET", target=f"/v1/artifacts/{artifact_id}/content"),
        )
        return self.resources.get_artifact_content(context, artifact_id)

    def exported_schema(self, context: TrustedContext, capability_id: str) -> dict[str, Any]:
        return self.gateway.registry.get(context, capability_id).public()
