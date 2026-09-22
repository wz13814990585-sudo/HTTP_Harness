"""A persisted Run scope ceiling survives actor privilege changes and restart."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from hnh.adapters.postgres.models import ActionRecord
from hnh.application.action_gateway import ActionGateway
from hnh.application.capabilities import CapabilityRegistry
from hnh.application.context import ContextBuilder
from hnh.application.resources import ResourceStore
from hnh.application.run_controller import RunController
from hnh.domain.errors import MigrationRequired, PermissionDenied
from hnh.domain.identity import TrustedContext


@pytest.mark.postgres
def test_run_scope_ceiling_prevents_post_admission_privilege_expansion(
    clean_postgres: Engine,
) -> None:
    admitted = TrustedContext(
        "tenant-p08-scope",
        "subject-p08-scope",
        frozenset({"runs:read", "integrations:invoke"}),
    )
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        admitted,
        {"agent_id": "scope-test", "input": "read only"},
        "p08-scope-ceiling",
    ).resource_id
    assert controller.get_run(admitted, run_id).admitted_scopes == admitted.scopes

    expanded = TrustedContext(
        admitted.tenant_id,
        admitted.subject_id,
        admitted.scopes | {"workspace:write", "artifacts:write"},
    )
    resumed = RunController(clean_postgres)
    assert resumed.effective_context(expanded, run_id).scopes == admitted.scopes
    gateway = ActionGateway(
        clean_postgres, CapabilityRegistry(), ResourceStore(clean_postgres), resumed
    )
    with pytest.raises(PermissionDenied):
        gateway.execute(
            expanded,
            {
                "method": "PUT",
                "target": "/v1/workspaces/ws-p08/files/forbidden.txt",
                "headers": {"content-type": "text/plain", "if-none-match": "*"},
                "payload": {"kind": "text", "text": "must not write"},
            },
            idempotency_key="p08-scope-forbidden",
            transport_kind="inproc",
            run_id=run_id,
        )
    with Session(clean_postgres) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(ActionRecord).where(ActionRecord.run_id == run_id)
            )
            == 0
        )

    revoked = TrustedContext(admitted.tenant_id, admitted.subject_id, frozenset({"runs:read"}))
    assert resumed.effective_context(revoked, run_id).scopes == frozenset({"runs:read"})


@pytest.mark.postgres
def test_context_snapshot_refuses_reuse_after_scope_revocation(clean_postgres: Engine) -> None:
    admitted = TrustedContext(
        "tenant-p08-context",
        "subject-p08-context",
        frozenset({"runs:read", "integrations:invoke"}),
    )
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        admitted,
        {"agent_id": "scope-test", "input": "echo"},
        "p08-context-scope",
    ).resource_id
    builder = ContextBuilder(controller, CapabilityRegistry())
    first = builder.build(admitted, run_id, 1)
    assert first.content["scope_fingerprint"]
    assert builder.build(admitted, run_id, 1).id == first.id
    revoked = TrustedContext(admitted.tenant_id, admitted.subject_id, frozenset({"runs:read"}))
    with pytest.raises(MigrationRequired, match="authorization scope changed"):
        builder.build(revoked, run_id, 1)
