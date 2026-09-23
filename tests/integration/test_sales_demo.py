from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import Engine

from hnh.adapters.execution.docker import DockerExecutionBroker
from hnh.ports.execution import SandboxProfile
from hnh.sales_demo import EXPECTED_REPORT, run_sales_demo


@pytest.mark.docker
@pytest.mark.postgres
def test_sales_demo_uses_isolated_python_approval_and_safe_reconciliation(
    clean_postgres: Engine,
    tmp_path: Path,
) -> None:
    image = os.environ.get("HNH_TEST_SANDBOX_IMAGE")
    if not image:
        pytest.skip("blocked_environment: HNH_TEST_SANDBOX_IMAGE is required")
    profile_id = "sales-demo-test"
    broker = DockerExecutionBroker(
        (
            SandboxProfile(
                profile_id,
                image,
                timeout_seconds=5.0,
                workspace_bytes=8 * 1024 * 1024,
            ),
        )
    )
    try:
        summary = run_sales_demo(
            clean_postgres,
            tmp_path / "blobs",
            broker,
            profile_id=profile_id,
            approve=lambda _view: True,
        )
    finally:
        broker.close()

    assert summary["verified"] is True
    assert summary["run_status"] == "succeeded"
    assert summary["csv_rows"] == 6
    assert summary["total_units"] == 40
    assert summary["total_revenue"] == "6010.00"
    assert summary["report_media_type"] == "text/markdown"
    assert summary["report_markdown"] == EXPECTED_REPORT
    assert summary["approval_status"] == "answered"
    assert summary["status_after_response_loss"] == "blocked"
    assert summary["action_status_after_response_loss"] == "outcome_unknown"
    assert summary["reconciliation"] == "confirmed_applied"
    assert summary["publish_requests"] == 1
    assert summary["publication_count"] == 1
    assert summary["replay_without_redispatch"] is True
    assert len(summary["action_ids"]) == 4
