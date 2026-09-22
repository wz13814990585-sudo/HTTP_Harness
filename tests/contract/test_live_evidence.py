"""Validate checked-in, sanitized evidence from optional live provider runs."""

from __future__ import annotations

import json
import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = REPOSITORY_ROOT / "reports" / "evidence"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _load(name: str) -> dict[str, object]:
    value = json.loads((EVIDENCE_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


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
