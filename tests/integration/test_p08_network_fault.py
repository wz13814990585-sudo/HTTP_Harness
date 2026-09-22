from __future__ import annotations

import pytest
from sqlalchemy import Engine

from hnh.application.recovery import RecoveryCoordinator
from hnh.application.run_controller import RunController
from hnh.domain.errors import VersionConflict
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus, RunStatus
from tests.support.effect_server import ControlledEffectServer, ControlledHTTPEffectDriver

CONTEXT = TrustedContext(
    "tenant-p08-fault",
    "subject-p08-fault",
    frozenset({"runs:read", "runs:write", "actions:read", "actions:reconcile"}),
)


@pytest.mark.postgres
def test_response_lost_after_real_http_effect_requires_query_and_never_redispatches(
    clean_postgres: Engine,
) -> None:
    controller = RunController(clean_postgres)
    run_id = controller.admit_run(
        CONTEXT,
        {"agent_id": "agent-p08", "input": "publish one report"},
        "p08-real-http-loss",
    ).resource_id
    controller.ensure_run_running(run_id, actor_ref="test")
    action_id = controller.record_admitted_action(
        CONTEXT,
        run_id,
        turn_id=1,
        call_index=0,
        request_hash="b" * 64,
        capability_id="test.publish",
        capability_revision="1",
        effect_semantics="unsafe",
        bound_operation={"report_id": "report-p08", "body_sha256": "c" * 64},
    )

    with ControlledEffectServer(drop_first_response=True) as server:
        driver = ControlledHTTPEffectDriver(server.base_url)
        try:
            first = RecoveryCoordinator(controller).dispatch(CONTEXT, action_id, driver)
            assert first.decision == "reconcile_only"
            assert first.action.status == ActionStatus.OUTCOME_UNKNOWN
            assert first.action.result is not None
            assert "transport_error" in first.action.result
            assert controller.get_run(CONTEXT, run_id).status == RunStatus.BLOCKED
            assert len(server.ledger.publications) == 1
            assert server.ledger.requests == 1

            # Rebuilding the kernel simulates a process restart. An unsafe
            # Action in outcome_unknown cannot be sent again by a new worker.
            restarted = RunController(clean_postgres)
            with pytest.raises(VersionConflict):
                RecoveryCoordinator(restarted).dispatch(CONTEXT, action_id, driver)
            assert server.ledger.requests == 1

            settled = RecoveryCoordinator(restarted).reconcile(
                CONTEXT,
                action_id,
                driver,
                idempotency_key="p08-query-ledger-once",
            )
            assert settled.action.status == ActionStatus.SUCCEEDED
            assert settled.decision == "confirmed_applied"
            assert server.ledger.requests == 1
            assert server.ledger.queries == 1
            assert controller.get_run(CONTEXT, run_id).status == RunStatus.QUEUED
        finally:
            driver.close()
