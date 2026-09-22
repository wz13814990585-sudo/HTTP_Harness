from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Protocol

import anyio

StrategyHint = Literal["simple", "plan_hint"]
ALLOWED_HINTS: frozenset[str] = frozenset({"simple", "plan_hint"})


@dataclass(frozen=True, slots=True)
class DecisionSuggestion:
    label: str
    confidence: float
    distribution: dict[str, float]
    provider: str
    version: str


class DecisionProvider(Protocol):
    async def classify(self, goal: str, allowed_labels: frozenset[str]) -> DecisionSuggestion: ...


@dataclass(frozen=True, slots=True)
class RoutedDecision:
    strategy_hint: StrategyHint
    reason: str
    provider: str | None = None
    version: str | None = None
    confidence: float | None = None

    def public(self) -> dict[str, str | float | None]:
        return {
            "strategy_hint": self.strategy_hint,
            "reason": self.reason,
            "provider": self.provider,
            "version": self.version,
            "confidence": self.confidence,
        }


class DecisionRouter:
    """Optional, bounded routing hint. It has no policy or state authority."""

    def __init__(
        self,
        provider: DecisionProvider | None = None,
        *,
        timeout_seconds: float = 1.0,
        min_confidence: float = 0.8,
    ) -> None:
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("classifier timeout must be positive")
        if (
            type(min_confidence) not in (int, float)
            or not math.isfinite(min_confidence)
            or not 0 < min_confidence <= 1
        ):
            raise ValueError("classifier threshold must be within (0, 1]")
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.min_confidence = min_confidence

    def decide(self, goal: str) -> RoutedDecision:
        if self.provider is None:
            return RoutedDecision("simple", "disabled")
        try:
            suggestion = anyio.run(self._classify, goal)
        except TimeoutError:
            return RoutedDecision("simple", "timeout")
        except Exception:
            return RoutedDecision("simple", "provider_failed")
        if not self._valid(suggestion):
            return RoutedDecision("simple", "invalid_output")
        if suggestion.confidence < self.min_confidence:
            return RoutedDecision(
                "simple",
                "low_confidence",
                suggestion.provider,
                suggestion.version,
                suggestion.confidence,
            )
        return RoutedDecision(
            suggestion.label,  # type: ignore[arg-type]
            "accepted_hint",
            suggestion.provider,
            suggestion.version,
            suggestion.confidence,
        )

    async def _classify(self, goal: str) -> DecisionSuggestion:
        assert self.provider is not None
        with anyio.fail_after(self.timeout_seconds):
            return await self.provider.classify(goal, ALLOWED_HINTS)

    @staticmethod
    def _valid(suggestion: object) -> bool:
        if not isinstance(suggestion, DecisionSuggestion):
            return False
        if not isinstance(suggestion.label, str) or suggestion.label not in ALLOWED_HINTS:
            return False
        values = suggestion.distribution
        if not isinstance(values, dict) or set(values) != ALLOWED_HINTS:
            return False
        if any(
            type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1
            for value in values.values()
        ):
            return False
        if not math.isclose(sum(values.values()), 1.0, abs_tol=0.01):
            return False
        if (
            type(suggestion.confidence) not in (int, float)
            or not math.isfinite(suggestion.confidence)
            or not 0 <= suggestion.confidence <= 1
        ):
            return False
        if not math.isclose(values[suggestion.label], suggestion.confidence, abs_tol=0.01):
            return False
        return (
            isinstance(suggestion.provider, str)
            and bool(suggestion.provider)
            and isinstance(suggestion.version, str)
            and bool(suggestion.version)
        )
