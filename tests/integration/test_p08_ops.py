from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from hnh.adapters.blobs.filesystem import FileBlobStore
from hnh.application.resources import ResourceStore
from hnh.application.run_controller import RunController
from hnh.backup import backup, restore
from hnh.config import Settings
from hnh.domain.identity import TrustedContext
from hnh.domain.states import ActionStatus, RunStatus
from hnh.transport.http.app import create_app

CONTEXT = TrustedContext(
    "tenant-p08", "subject-p08", frozenset({"runs:read", "artifacts:read", "artifacts:write"})
)


@pytest.mark.postgres
def test_at_067_readiness_distinguishes_healthy_database_from_missing_optional_services(
    clean_postgres: Engine,
) -> None:
    response = TestClient(
        create_app(
            settings=Settings(database_url=None, development_token=None),
            engine=clean_postgres,
        )
    ).get("/readyz")
    assert response.status_code == 200
    assert response.json()["core_ready"] is True
    assert response.json()["components"]["database"] == "ready"
    assert response.json()["components"]["isolated_broker_configuration"] == "missing"
    assert response.json()["components"]["model_configuration"] == "missing"


@pytest.mark.postgres
def test_at_066_backup_restores_active_terminal_and_blob_without_starting_workers(
    clean_postgres: Engine, tmp_path: Path
) -> None:
    source_url = clean_postgres.url.render_as_string(hide_password=False)
    controller = RunController(clean_postgres)
    active_id = controller.admit_run(
        CONTEXT, {"agent_id": "backup", "input": "resume me"}, "backup-active"
    ).resource_id
    controller.ensure_run_running(active_id, actor_ref="test")
    unknown_id = controller.get_or_record_admitted_action(
        CONTEXT,
        active_id,
        turn_id=1,
        call_index=0,
        request_hash="a" * 64,
        capability_id="test.publish",
        capability_revision="1",
        effect_semantics="unsafe",
        bound_operation={"target": "external-publication"},
    ).action.id
    assert controller.begin_action_dispatch(unknown_id)
    controller.mark_action_outcome_unknown(
        unknown_id, receipt={"possible_dispatch": True}, upstream_handle=None
    )
    finished_id = controller.admit_run(
        CONTEXT, {"agent_id": "backup", "input": "finished"}, "backup-finished"
    ).resource_id
    controller.ensure_run_running(finished_id, actor_ref="test")
    controller.fail_run(finished_id, code="test_finished", detail="intentional")
    blob_root = tmp_path / "source-blobs"
    artifact = ResourceStore(clean_postgres, FileBlobStore(blob_root)).create_artifact(
        CONTEXT, b"restore evidence", "text/plain", "backup-artifact"
    )
    backup_path = tmp_path / "snapshot"
    manifest = backup(source_url, blob_root, backup_path)
    assert manifest["format"] == "hnh-offline-backup-v1"
    assert len(manifest["blobs"]) == 1

    database_name = f"hnh_restore_{uuid4().hex[:12]}"
    admin_url = make_url(source_url).set(database="postgres")
    target_url = make_url(source_url).set(database=database_name)
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        target_blob_root = tmp_path / "restored-blobs"
        report = restore(
            backup_path, target_url.render_as_string(hide_password=False), target_blob_root
        )
        assert report == {
            "active_runs_requiring_recovery_review": 1,
            "unknown_actions_requiring_reconciliation": 1,
            "workers_started": False,
        }
        restored_engine = create_engine(target_url)
        try:
            restored_controller = RunController(restored_engine)
            assert restored_controller.get_run(CONTEXT, active_id).status == RunStatus.BLOCKED
            assert restored_controller.get_run(CONTEXT, finished_id).status == RunStatus.FAILED
            assert (
                restored_controller.get_action(CONTEXT, unknown_id).status
                == ActionStatus.OUTCOME_UNKNOWN
            )
            assert restored_controller.begin_action_dispatch(unknown_id) is False
            assert (
                ResourceStore(
                    restored_engine, FileBlobStore(target_blob_root)
                ).get_artifact_content(CONTEXT, artifact.id)[0]
                == b"restore evidence"
            )
        finally:
            restored_engine.dispose()
        with pytest.raises(ValueError):
            restore(
                backup_path,
                target_url.render_as_string(hide_password=False),
                tmp_path / "another-restore",
            )
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin.dispose()
