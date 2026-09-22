from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from hnh.adapters.postgres.database import build_engine


@pytest.fixture(scope="session")
def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def postgres_engine() -> Engine:
    database_url = os.environ.get("HNH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("HNH_TEST_DATABASE_URL is required for real PostgreSQL tests")
    engine = build_engine(database_url)
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    yield engine
    engine.dispose()


@pytest.fixture
def clean_postgres(postgres_engine: Engine) -> Engine:
    tables = (
        "mcp_rpc_calls, mcp_continuations, mcp_tasks, "
        "input_responses, reconciliations, checkpoints, input_requests, "
        "executions, execution_sessions, completion_records, context_snapshots, "
        "budget_reservations, model_call_attempts, "
        "workspace_entries, artifacts, http_exchanges, action_attempts, jobs, model_calls, "
        "events, budgets, actions, idempotency_records, runs"
    )
    with postgres_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} CASCADE"))
    yield postgres_engine
    with postgres_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} CASCADE"))
