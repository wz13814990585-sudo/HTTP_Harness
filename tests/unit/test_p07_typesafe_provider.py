from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from hnh.adapters.decision.typesafe import (
    TypeSafeDecisionError,
    TypeSafeDecisionProvider,
)
from hnh.application.decision import DecisionRouter
from hnh.config import Settings


def test_typesafe_choice_contract_and_confidence_semantics() -> None:
    captured: dict[str, Any] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    "strategy": {
                        "type": "choice",
                        "choice": "plan_hint",
                        # TypeSafe confidence is not the selected probability.
                        "confidence": 0.78,
                        "probabilities": {"simple": 0.15, "plan_hint": 0.85},
                    }
                },
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        )

    provider = TypeSafeDecisionProvider(
        api_key="typesafe-test-secret",
        model="jev-latest",
        endpoint="https://typesafe.example.test/v1/systemone",
        transport=httpx.MockTransport(respond),
    )
    decision = DecisionRouter(provider, min_confidence=0.75).decide(
        "Prepare a report from several dependent sources."
    )

    assert captured["url"] == "https://typesafe.example.test/v1/systemone"
    assert captured["authorization"] == "Bearer typesafe-test-secret"
    body = captured["body"]
    assert body["model"] == "jev-latest"
    assert body["questions"]["strategy"]["type"] == "choice"
    assert set(body["questions"]["strategy"]["criteria"]) == {"simple", "plan_hint"}
    assert decision.strategy_hint == "plan_hint"
    assert decision.reason == "accepted_hint"
    assert decision.provider == "typesafe-systemone"
    assert decision.version == "jev-1.13.0"
    assert decision.confidence == 0.78
    assert "typesafe-test-secret" not in json.dumps(decision.public())


def test_typesafe_low_confidence_falls_back_to_simple() -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "jev-test",
                "answers": {
                    "strategy": {
                        "type": "choice",
                        "choice": "plan_hint",
                        "confidence": 0.4,
                        "probabilities": {"simple": 0.2, "plan_hint": 0.8},
                    }
                },
            },
        )

    provider = TypeSafeDecisionProvider(api_key="test", transport=httpx.MockTransport(respond))
    decision = DecisionRouter(provider, min_confidence=0.8).decide("Use several steps")
    assert decision.strategy_hint == "simple"
    assert decision.reason == "low_confidence"
    assert decision.provider == "typesafe-systemone"


def test_typesafe_malformed_or_failed_response_is_sanitized_fallback() -> None:
    secret = "must-never-appear"

    def malformed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answers": {"strategy": {"type": "choice"}}})

    provider = TypeSafeDecisionProvider(api_key=secret, transport=httpx.MockTransport(malformed))
    decision = DecisionRouter(provider).decide("Classify this")
    assert decision.reason == "provider_failed"
    assert secret not in json.dumps(decision.public())

    def failed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": secret})

    failing_provider = TypeSafeDecisionProvider(
        api_key=secret, transport=httpx.MockTransport(failed)
    )
    with pytest.raises(TypeSafeDecisionError) as error:
        import anyio

        anyio.run(failing_provider.classify, "Classify this", frozenset({"simple", "plan_hint"}))
    assert secret not in str(error.value)


def test_typesafe_selected_label_must_be_a_distribution_maximum() -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "jev-test",
                "answers": {
                    "strategy": {
                        "type": "choice",
                        "choice": "simple",
                        "confidence": 0.9,
                        "probabilities": {"simple": 0.1, "plan_hint": 0.9},
                    }
                },
            },
        )

    provider = TypeSafeDecisionProvider(api_key="test", transport=httpx.MockTransport(respond))
    decision = DecisionRouter(provider).decide("Classify this")
    assert decision.strategy_hint == "simple"
    assert decision.reason == "invalid_output"


def test_typesafe_settings_are_optional_but_strict_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = (
        "TYPESAFE_API_KEY",
        "HNH_TYPESAFE_ENABLED",
        "HNH_TYPESAFE_MODEL",
        "HNH_TYPESAFE_BASE_URL",
        "HNH_TYPESAFE_TIMEOUT_SECONDS",
        "HNH_TYPESAFE_MIN_CONFIDENCE",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    assert Settings.from_environment().typesafe_enabled is False

    monkeypatch.setenv("HNH_TYPESAFE_ENABLED", "1")
    with pytest.raises(ValueError, match="TYPESAFE_API_KEY"):
        Settings.from_environment()

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-secret")
    monkeypatch.setenv("HNH_TYPESAFE_MODEL", "jev-latest")
    monkeypatch.setenv("HNH_TYPESAFE_BASE_URL", "https://api.typesafe.ai/v1/systemone")
    monkeypatch.setenv("HNH_TYPESAFE_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("HNH_TYPESAFE_MIN_CONFIDENCE", "0.85")
    settings = Settings.from_environment()
    assert settings.typesafe_enabled is True
    assert settings.typesafe_model == "jev-latest"
    assert settings.typesafe_timeout_seconds == 3.0
    assert settings.typesafe_min_confidence == 0.85

    monkeypatch.setenv("HNH_TYPESAFE_ENABLED", "sometimes")
    with pytest.raises(ValueError, match="HNH_TYPESAFE_ENABLED"):
        Settings.from_environment()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.typesafe.ai/v1/systemone",
        "https://user:password@api.typesafe.ai/v1/systemone",
        "https://api.typesafe.ai/v1/systemone?token=secret",
    ],
)
def test_typesafe_endpoint_rejects_unsafe_configuration(endpoint: str) -> None:
    with pytest.raises(ValueError, match="HTTPS URL"):
        TypeSafeDecisionProvider(api_key="test", endpoint=endpoint)
