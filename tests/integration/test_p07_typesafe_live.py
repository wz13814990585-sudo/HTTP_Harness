from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hnh.adapters.decision.typesafe import TypeSafeDecisionProvider
from hnh.application.decision import DecisionRouter


@pytest.mark.live
def test_live_typesafe_strategy_choice_matches_guarded_router_contract() -> None:
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if os.environ.get("HNH_RUN_LIVE_TESTS") != "1" or not api_key:
        pytest.skip(
            "blocked_environment: set HNH_RUN_LIVE_TESTS=1 and TYPESAFE_API_KEY "
            "for the live TypeSafe contract"
        )
    timeout = float(os.environ.get("HNH_TYPESAFE_TIMEOUT_SECONDS", "2"))
    threshold = float(os.environ.get("HNH_TYPESAFE_MIN_CONFIDENCE", "0.8"))
    provider = TypeSafeDecisionProvider(
        api_key=api_key,
        model=os.environ.get("HNH_TYPESAFE_MODEL", "jev-latest"),
        endpoint=os.environ.get("HNH_TYPESAFE_BASE_URL", "https://api.typesafe.ai/v1/systemone"),
        request_timeout_seconds=timeout,
    )
    decision = DecisionRouter(
        provider,
        timeout_seconds=timeout,
        min_confidence=threshold,
    ).decide("Read one short authorized text file and return its existing contents without edits.")
    assert decision.reason in {"accepted_hint", "low_confidence"}
    assert decision.provider == "typesafe-systemone"
    assert decision.version
    assert decision.strategy_hint in {"simple", "plan_hint"}
    evidence_output = os.environ.get("HNH_TYPESAFE_EVIDENCE_OUTPUT")
    if evidence_output:
        Path(evidence_output).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "implementation_revision": os.environ.get(
                        "HNH_EVAL_IMPLEMENTATION_REVISION", "unrecorded"
                    ),
                    "provider": decision.provider,
                    "model_version": decision.version,
                    "strategy_hint": decision.strategy_hint,
                    "routing_reason": decision.reason,
                    "confidence": decision.confidence,
                    "credential_recorded": False,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
