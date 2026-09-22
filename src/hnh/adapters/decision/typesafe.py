from __future__ import annotations

import math
from typing import Any

import httpx

from hnh.application.decision import DecisionSuggestion

TYPESAFE_SYSTEMONE_URL = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_DEFAULT_MODEL = "jev-latest"
TYPESAFE_STRATEGY_LABELS = frozenset({"simple", "plan_hint"})


class TypeSafeDecisionError(RuntimeError):
    """Sanitized optional-classifier failure; never includes credentials or response bodies."""


def validate_typesafe_configuration(
    *,
    enabled: bool,
    api_key: str | None,
    model: str,
    endpoint: str,
    timeout_seconds: float,
    min_confidence: float,
) -> None:
    if type(enabled) is not bool:
        raise ValueError("TypeSafe enabled flag must be boolean")
    if enabled and not api_key:
        raise ValueError("TYPESAFE_API_KEY is required when HNH_TYPESAFE_ENABLED=1")
    if not model.strip():
        raise ValueError("HNH_TYPESAFE_MODEL must not be empty")
    try:
        url = httpx.URL(endpoint)
    except (TypeError, ValueError) as exc:
        raise ValueError("HNH_TYPESAFE_BASE_URL must be a valid HTTPS URL") from exc
    if (
        url.scheme != "https"
        or not url.host
        or bool(url.username)
        or bool(url.password)
        or url.query
        or url.fragment
    ):
        raise ValueError("HNH_TYPESAFE_BASE_URL must be an HTTPS URL without credentials/query")
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("HNH_TYPESAFE_TIMEOUT_SECONDS must be positive")
    if (
        type(min_confidence) not in (int, float)
        or not math.isfinite(min_confidence)
        or not 0 < min_confidence <= 1
    ):
        raise ValueError("HNH_TYPESAFE_MIN_CONFIDENCE must be within (0, 1]")


class TypeSafeDecisionProvider:
    """TypeSafe System One Choice adapter for a non-authoritative strategy hint."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = TYPESAFE_DEFAULT_MODEL,
        endpoint: str = TYPESAFE_SYSTEMONE_URL,
        request_timeout_seconds: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        validate_typesafe_configuration(
            enabled=True,
            api_key=api_key,
            model=model,
            endpoint=endpoint,
            timeout_seconds=request_timeout_seconds,
            min_confidence=1.0,
        )
        self._api_key = api_key
        self._model = model
        self._endpoint = endpoint
        self._request_timeout_seconds = request_timeout_seconds
        self._transport = transport

    async def classify(
        self,
        goal: str,
        allowed_labels: frozenset[str],
    ) -> DecisionSuggestion:
        if allowed_labels != TYPESAFE_STRATEGY_LABELS:
            raise TypeSafeDecisionError("TypeSafe strategy labels do not match the pinned contract")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 65_536:
            raise TypeSafeDecisionError("TypeSafe state must be non-empty and bounded")
        request_body = {
            "state": goal,
            "model": self._model,
            "questions": {
                "strategy": {
                    "type": "choice",
                    "instructions": (
                        "Choose the smallest execution strategy suitable for this task. "
                        "This is only a routing hint and must not decide authorization, "
                        "approval, retry safety, or task completion."
                    ),
                    "criteria": {
                        "simple": (
                            "A direct bounded model-tool loop is sufficient; no explicit "
                            "multi-step planning hint is needed."
                        ),
                        "plan_hint": (
                            "The task has several dependent steps or constraints and would "
                            "benefit from an explicit plan, while still using the same kernel."
                        ),
                    },
                }
            },
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._request_timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=request_body,
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise TypeSafeDecisionError("TypeSafe System One request failed") from None
        return self._parse_response(payload)

    @staticmethod
    def _parse_response(payload: Any) -> DecisionSuggestion:
        if not isinstance(payload, dict):
            raise TypeSafeDecisionError("TypeSafe response was not an object")
        answers = payload.get("answers")
        answer = answers.get("strategy") if isinstance(answers, dict) else None
        version = payload.get("model")
        if (
            not isinstance(answer, dict)
            or answer.get("type") != "choice"
            or not isinstance(answer.get("choice"), str)
            or type(answer.get("confidence")) not in (int, float)
            or not isinstance(answer.get("probabilities"), dict)
            or not isinstance(version, str)
            or not version
        ):
            raise TypeSafeDecisionError("TypeSafe response did not match the Choice contract")
        distribution: dict[str, float] = {}
        for label, value in answer["probabilities"].items():
            if not isinstance(label, str) or type(value) not in (int, float):
                raise TypeSafeDecisionError("TypeSafe probabilities were malformed")
            distribution[label] = float(value)
        return DecisionSuggestion(
            label=answer["choice"],
            confidence=float(answer["confidence"]),
            distribution=distribution,
            provider="typesafe-systemone",
            version=version,
        )
