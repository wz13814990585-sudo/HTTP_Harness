"""Validate checked-in, sanitized evidence from optional live provider runs."""

from __future__ import annotations

import json
import re
from pathlib import Path

from hnh.evaluation import summarize

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = REPOSITORY_ROOT / "reports" / "evidence"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _load(name: str) -> dict[str, object]:
    value = json.loads((EVIDENCE_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _load_jsonl(name: str) -> list[dict[str, object]]:
    rows = [
        json.loads(line)
        for line in (REPOSITORY_ROOT / "reports" / "evaluation" / name)
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    assert rows and all(isinstance(row, dict) for row in rows)
    return rows


def test_at_030_recorded_live_evidence_is_sanitized_and_complete() -> None:
    evidence = _load("at030_deepseek_20260923.json")

    assert evidence["schema_version"] == 1
    assert evidence["test"] == "AT-030"
    assert evidence["provider"] == "deepseek-responses"
    assert evidence["model"] == "deepseek-flash"
    assert evidence["reasoning_effort"] == "none"
    assert evidence["run_status"] == "succeeded"
    assert str(evidence["implementation_revision"]).startswith("git:")

    artifact = evidence["artifact"]
    assert isinstance(artifact, dict)
    assert artifact == {
        "media_type": "text/plain",
        "sha256": "b7baa74bdf27b597507422b28b42c3c478559b420e139300b901cf38d6e1b4ff",
        "size_bytes": 19,
    }
    assert evidence["action_capabilities"] == ["workspace.file.read", "artifact.create"]

    model_calls = evidence["model_calls"]
    assert isinstance(model_calls, int) and model_calls >= 2
    request_hashes = evidence["provider_request_hashes"]
    assert isinstance(request_hashes, list) and len(request_hashes) == model_calls
    assert all(isinstance(item, str) and SHA256_PATTERN.fullmatch(item) for item in request_hashes)

    usage = evidence["usage"]
    assert isinstance(usage, dict)
    assert all(isinstance(usage[key], int) and usage[key] > 0 for key in usage)
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]
    assert "api_key" not in json.dumps(evidence).lower()


def test_recorded_typesafe_evidence_is_sanitized_and_within_router_contract() -> None:
    evidence = _load("typesafe_strategy_20260923.json")

    assert evidence["schema_version"] == 1
    assert evidence["provider"] == "typesafe-systemone"
    assert str(evidence["model_version"]).startswith("jev-")
    assert evidence["strategy_hint"] in {"simple", "plan_hint"}
    assert evidence["routing_reason"] in {"accepted_hint", "low_confidence"}
    assert isinstance(evidence["confidence"], (int, float))
    assert 0.0 <= evidence["confidence"] <= 1.0
    assert evidence["credential_recorded"] is False
    assert str(evidence["implementation_revision"]).startswith("git:")
    assert "api_key" not in json.dumps(evidence).lower()


def test_at_068_recorded_live_task_and_skills_evidence_retains_all_outcomes() -> None:
    task_rows = _load_jsonl("live_tasks_deepseek_20260923.raw.jsonl")
    task_summary = summarize(task_rows)
    assert task_summary["design_complete"] is True
    assert task_summary["design_kind"] == "task_suite"
    assert task_summary["evidence_tier"] == "live_provider"
    assert task_summary["denominator"] == 3
    assert task_summary["evidence_based_completion_count"] == 1
    assert task_summary["failed_count"] == 2
    assert task_summary["provider_usage_sample_count"] == 3
    assert task_summary["provider_input_tokens"] == 22590
    assert task_summary["provider_output_tokens"] == 1341
    assert {row["case_id"] for row in task_rows} == {
        "transform-upper-001",
        "transform-lines-002",
        "transform-sum-003",
    }
    assert [row["failure_reason"] for row in task_rows].count("model_output_invalid") == 2

    repair_rows = _load_jsonl("live_tasks_deepseek_20260923_repair1.raw.jsonl")
    repair_summary = summarize(repair_rows)
    assert repair_summary["design_complete"] is True
    assert repair_summary["design_kind"] == "task_suite"
    assert repair_summary["evidence_tier"] == "live_provider"
    assert repair_summary["denominator"] == 3
    assert repair_summary["evidence_based_completion_count"] == 1
    assert repair_summary["failed_count"] == 1
    assert repair_summary["false_completion_count"] == 1
    assert repair_summary["provider_usage_sample_count"] == 3
    assert repair_summary["provider_input_tokens"] == 26430
    assert repair_summary["provider_output_tokens"] == 1932
    assert [row["failure_reason"] for row in repair_rows].count("model_output_invalid") == 1
    assert sum(row["claimed_complete"] is True for row in repair_rows) == 2
    assert sum(row["verified_completion"] is True for row in repair_rows) == 1

    skill_rows = _load_jsonl("live_skills_deepseek_20260923.raw.jsonl")
    skill_summary = summarize(skill_rows)
    assert skill_summary["design_complete"] is True
    assert skill_summary["design_kind"] == "ablation"
    assert skill_summary["evidence_tier"] == "live_provider"
    assert skill_summary["denominator"] == 8
    assert skill_summary["evidence_based_completion_count"] == 8
    assert skill_summary["failed_count"] == 0
    by_condition = skill_summary["by_condition"]
    assert isinstance(by_condition, dict)
    off = by_condition["off"]
    on = by_condition["on"]
    assert isinstance(off, dict) and isinstance(on, dict)
    assert off["denominator"] == 4
    assert on["denominator"] == 4
    assert off["provider_input_tokens"] == 12251
    assert on["provider_input_tokens"] == 14408

    all_rows = task_rows + repair_rows + skill_rows
    assert {row["model_revision"] for row in all_rows} == {"deepseek-flash"}
    assert all(row["model_usage_source"] == "provider_reported" for row in all_rows)
    assert all(row["environment_hash"] != "0" * 64 for row in all_rows)
    serialized = json.dumps(all_rows).lower()
    assert all(
        marker not in serialized
        for marker in ("api_key", "authorization", "bearer", "client_secret", "password")
    )
