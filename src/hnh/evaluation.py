"""Controlled evaluation records; this module never substitutes fake runs for live evidence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from hnh.application.security import redact
from hnh.domain.states import RunStatus

ModelSurface = Literal["native_function_schema", "http_semantic_operation"]
Downstream = Literal["native_http", "mcp_adapter"]
Outcome = Literal["succeeded", "failed", "timeout", "blocked"]
AblationFeature = Literal["on_demand_catalog", "classifier", "skills", "child_runs"]
EvidenceTier = Literal["controlled", "live_provider"]

ARMS: tuple[tuple[ModelSurface, Downstream], ...] = (
    ("native_function_schema", "native_http"),
    ("native_function_schema", "mcp_adapter"),
    ("http_semantic_operation", "native_http"),
    ("http_semantic_operation", "mcp_adapter"),
)
RUN_STATUS_VALUES = frozenset(status.value for status in RunStatus)


class EvaluationDesignError(ValueError):
    """A control/contract drift invalidates the experiment, not one task result."""


def stable_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    task: str
    expected_evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.case_id or not self.task or not self.expected_evidence:
            raise ValueError("evaluation case requires an ID, task, and predeclared oracle")


@dataclass(frozen=True, slots=True)
class EvaluationControls:
    model: str
    model_settings: dict[str, object]
    capability_revision: str
    policy_revision: str
    scopes: tuple[str, ...]
    budget: dict[str, int]
    timeout_seconds: float
    fixtures_hash: str
    arm_order_seed: int = 0
    environment_hash: str = "0" * 64
    evidence_tier: EvidenceTier = "controlled"

    def __post_init__(self) -> None:
        if not self.model or not self.capability_revision or not self.policy_revision:
            raise ValueError("evaluation control revisions must be explicit")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or not self.fixtures_hash
        ):
            raise ValueError("evaluation control timeout and fixture hash are required")
        if type(self.arm_order_seed) is not int or self.arm_order_seed < 0:
            raise ValueError("arm order seed must be a nonnegative integer")
        if (
            not isinstance(self.environment_hash, str)
            or len(self.environment_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.environment_hash)
        ):
            raise ValueError("evaluation environment hash must be a SHA-256 hex digest")
        if self.evidence_tier not in {"controlled", "live_provider"}:
            raise ValueError("evaluation evidence tier is invalid")
        if self.evidence_tier == "live_provider" and self.environment_hash == "0" * 64:
            raise ValueError("live-provider evidence requires explicit environment identity")


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    run_id: str | None
    outcome: Outcome
    accepted_evidence: tuple[str, ...]
    claimed_complete: bool
    model_input_tokens: int | None
    model_output_tokens: int | None
    model_usage_source: Literal["provider_reported", "unavailable"]
    unsafe_duplicate_effects: int = 0
    permission_violations: int = 0
    failure_reason: str | None = None
    model_call_ids: tuple[str, ...] = ()
    action_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    event_seq_start: int | None = None
    event_seq_end: int | None = None
    run_status: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"succeeded", "failed", "timeout", "blocked"}:
            raise ValueError("evaluation outcome is invalid")
        if self.run_status is not None and (
            not isinstance(self.run_status, str) or self.run_status not in RUN_STATUS_VALUES
        ):
            raise ValueError("evaluation Run status is invalid")
        if type(self.claimed_complete) is not bool:
            raise ValueError("completion claim must be boolean")
        if self.model_usage_source not in {"provider_reported", "unavailable"}:
            raise ValueError("model usage source is invalid")
        if self.model_usage_source == "provider_reported" and (
            self.model_input_tokens is None or self.model_output_tokens is None
        ):
            raise ValueError("provider-reported usage requires both token counts")
        if self.model_usage_source == "unavailable" and (
            self.model_input_tokens is not None or self.model_output_tokens is not None
        ):
            raise ValueError("unavailable token usage cannot contain estimates")
        for value in (
            self.model_input_tokens,
            self.model_output_tokens,
            self.unsafe_duplicate_effects,
            self.permission_violations,
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("evaluation counts must be nonnegative integers")
        for value in (self.event_seq_start, self.event_seq_end):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("evaluation event sequence must be a nonnegative integer")
        if (
            self.event_seq_start is not None
            and self.event_seq_end is not None
            and self.event_seq_start > self.event_seq_end
        ):
            raise ValueError("evaluation event sequence range is inverted")


class EvaluationExecutor(Protocol):
    def run(
        self,
        case: EvaluationCase,
        controls: EvaluationControls,
        surface: ModelSurface,
        downstream: Downstream,
    ) -> EvaluationResult: ...


class AblationExecutor(Protocol):
    def run(
        self,
        case: EvaluationCase,
        controls: EvaluationControls,
        feature: AblationFeature,
        enabled: bool,
    ) -> EvaluationResult: ...


class TaskSuiteExecutor(Protocol):
    def run(self, case: EvaluationCase, controls: EvaluationControls) -> EvaluationResult: ...


class ExperimentRunner:
    """Runs every task in all four arms with exactly the same control object."""

    def __init__(self, executor: EvaluationExecutor, output: Path) -> None:
        self.executor = executor
        self.output = output

    def run(
        self, cases: tuple[EvaluationCase, ...], controls: EvaluationControls
    ) -> list[dict[str, object]]:
        if not cases or len({case.case_id for case in cases}) != len(cases):
            raise ValueError("evaluation cases must be nonempty and uniquely identified")
        if self.output.exists():
            raise FileExistsError("raw evaluation file already exists; never overwrite prior runs")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        control_hash = stable_hash(asdict(controls))
        records: list[dict[str, object]] = []
        with self.output.open("x", encoding="utf-8") as stream:
            for case_index, case in enumerate(cases):
                case_hash = stable_hash(asdict(case))
                offset = (controls.arm_order_seed + case_index) % len(ARMS)
                ordered_arms = ARMS[offset:] + ARMS[:offset]
                for arm_index, (surface, downstream) in enumerate(ordered_arms):
                    started = time.monotonic()
                    try:
                        result = self.executor.run(case, controls, surface, downstream)
                    except EvaluationDesignError:
                        raise
                    except TimeoutError:
                        result = EvaluationResult(
                            None,
                            "timeout",
                            (),
                            False,
                            None,
                            None,
                            "unavailable",
                            failure_reason="executor timeout",
                        )
                    except Exception as exc:
                        result = EvaluationResult(
                            None,
                            "failed",
                            (),
                            False,
                            None,
                            None,
                            "unavailable",
                            failure_reason=f"executor error: {type(exc).__name__}",
                        )
                    if stable_hash(asdict(controls)) != control_hash:
                        raise ValueError("evaluation controls mutated during execution")
                    record: dict[str, object] = {
                        "case_id": case.case_id,
                        "case_index": case_index,
                        "arm_index": arm_index,
                        "arm_order_seed": controls.arm_order_seed,
                        "case_hash": case_hash,
                        "controls_hash": control_hash,
                        "model_revision": controls.model,
                        "model_settings_hash": stable_hash(controls.model_settings),
                        "capability_revision": controls.capability_revision,
                        "policy_revision": controls.policy_revision,
                        "fixtures_hash": controls.fixtures_hash,
                        "environment_hash": controls.environment_hash,
                        "evidence_tier": controls.evidence_tier,
                        "model_surface": surface,
                        "downstream": downstream,
                        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                        "oracle": list(case.expected_evidence),
                        "verified_completion": (
                            result.outcome == "succeeded"
                            and set(case.expected_evidence).issubset(result.accepted_evidence)
                        ),
                        **asdict(result),
                    }
                    safe_record = redact(record)
                    assert isinstance(safe_record, dict)
                    # The general redactor treats any key containing "token" as
                    # secret. These two validated integer usage counters are not
                    # credentials and must survive for real provider accounting.
                    safe_record["model_input_tokens"] = result.model_input_tokens
                    safe_record["model_output_tokens"] = result.model_output_tokens
                    stream.write(json.dumps(safe_record, ensure_ascii=False, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                    records.append(safe_record)
        return records


class AblationRunner:
    """Separate off/on experiment; never attributes combined changes to one factor."""

    def __init__(self, executor: AblationExecutor, output: Path) -> None:
        self.executor = executor
        self.output = output

    def run(
        self,
        cases: tuple[EvaluationCase, ...],
        controls: EvaluationControls,
        feature: AblationFeature,
    ) -> list[dict[str, object]]:
        if not cases or len({case.case_id for case in cases}) != len(cases):
            raise ValueError("ablation cases must be nonempty and uniquely identified")
        if self.output.exists():
            raise FileExistsError("raw ablation file already exists")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        control_hash = stable_hash(asdict(controls))
        records: list[dict[str, object]] = []
        with self.output.open("x", encoding="utf-8") as stream:
            for case_index, case in enumerate(cases):
                order = (
                    (False, True)
                    if (controls.arm_order_seed + case_index) % 2 == 0
                    else (True, False)
                )
                for condition_index, enabled in enumerate(order):
                    started = time.monotonic()
                    try:
                        result = self.executor.run(case, controls, feature, enabled)
                    except EvaluationDesignError:
                        raise
                    except TimeoutError:
                        result = EvaluationResult(
                            None,
                            "timeout",
                            (),
                            False,
                            None,
                            None,
                            "unavailable",
                            failure_reason="executor timeout",
                        )
                    except Exception as exc:
                        result = EvaluationResult(
                            None,
                            "failed",
                            (),
                            False,
                            None,
                            None,
                            "unavailable",
                            failure_reason=f"executor error: {type(exc).__name__}",
                        )
                    if stable_hash(asdict(controls)) != control_hash:
                        raise ValueError("ablation controls mutated during execution")
                    record = {
                        "case_id": case.case_id,
                        "case_index": case_index,
                        "condition_index": condition_index,
                        "arm_order_seed": controls.arm_order_seed,
                        "case_hash": stable_hash(asdict(case)),
                        "controls_hash": control_hash,
                        "model_revision": controls.model,
                        "model_settings_hash": stable_hash(controls.model_settings),
                        "capability_revision": controls.capability_revision,
                        "policy_revision": controls.policy_revision,
                        "fixtures_hash": controls.fixtures_hash,
                        "environment_hash": controls.environment_hash,
                        "evidence_tier": controls.evidence_tier,
                        "ablation_feature": feature,
                        "feature_enabled": enabled,
                        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                        "oracle": list(case.expected_evidence),
                        "verified_completion": (
                            result.outcome == "succeeded"
                            and set(case.expected_evidence).issubset(result.accepted_evidence)
                        ),
                        **asdict(result),
                    }
                    safe_record = redact(record)
                    assert isinstance(safe_record, dict)
                    safe_record["model_input_tokens"] = result.model_input_tokens
                    safe_record["model_output_tokens"] = result.model_output_tokens
                    stream.write(json.dumps(safe_record, ensure_ascii=False, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                    records.append(safe_record)
        return records


class TaskSuiteRunner:
    """Runs each predeclared task once; keeps failed and interrupted attempts."""

    def __init__(self, executor: TaskSuiteExecutor, output: Path) -> None:
        self.executor = executor
        self.output = output

    def run(
        self, cases: tuple[EvaluationCase, ...], controls: EvaluationControls
    ) -> list[dict[str, object]]:
        if not cases or len({case.case_id for case in cases}) != len(cases):
            raise ValueError("task suite cases must be nonempty and uniquely identified")
        if self.output.exists():
            raise FileExistsError("raw task suite file already exists")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        control_hash = stable_hash(asdict(controls))
        suite_hash = stable_hash([asdict(case) for case in cases])
        records: list[dict[str, object]] = []
        with self.output.open("x", encoding="utf-8") as stream:
            for case_index, case in enumerate(cases):
                started = time.monotonic()
                try:
                    result = self.executor.run(case, controls)
                except EvaluationDesignError:
                    raise
                except TimeoutError:
                    result = EvaluationResult(
                        None,
                        "timeout",
                        (),
                        False,
                        None,
                        None,
                        "unavailable",
                        failure_reason="executor timeout",
                    )
                except Exception as exc:
                    result = EvaluationResult(
                        None,
                        "failed",
                        (),
                        False,
                        None,
                        None,
                        "unavailable",
                        failure_reason=f"executor error: {type(exc).__name__}",
                    )
                if stable_hash(asdict(controls)) != control_hash:
                    raise ValueError("task suite controls mutated during execution")
                record: dict[str, object] = {
                    "evaluation_kind": "task_suite",
                    "case_id": case.case_id,
                    "case_index": case_index,
                    "case_hash": stable_hash(asdict(case)),
                    "suite_hash": suite_hash,
                    "suite_size": len(cases),
                    "controls_hash": control_hash,
                    "model_revision": controls.model,
                    "model_settings_hash": stable_hash(controls.model_settings),
                    "capability_revision": controls.capability_revision,
                    "policy_revision": controls.policy_revision,
                    "fixtures_hash": controls.fixtures_hash,
                    "environment_hash": controls.environment_hash,
                    "evidence_tier": controls.evidence_tier,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                    "oracle": list(case.expected_evidence),
                    "verified_completion": (
                        result.outcome == "succeeded"
                        and set(case.expected_evidence).issubset(result.accepted_evidence)
                    ),
                    **asdict(result),
                }
                safe_record = redact(record)
                assert isinstance(safe_record, dict)
                safe_record["model_input_tokens"] = result.model_input_tokens
                safe_record["model_output_tokens"] = result.model_output_tokens
                stream.write(json.dumps(safe_record, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                records.append(safe_record)
        return records


def summarize(records: list[dict[str, object]]) -> dict[str, object]:
    """Failures, timeouts and blocked cases always stay in the denominator."""
    if not records:
        raise ValueError("raw evaluation records are empty")
    if any(
        record.get("outcome") not in {"succeeded", "failed", "timeout", "blocked"}
        for record in records
    ):
        raise ValueError("unknown outcome in raw evaluation records")
    for record in records:
        oracle = record.get("oracle")
        evidence = record.get("accepted_evidence")
        claim = record.get("claimed_complete")
        verified_claim = record.get("verified_completion")
        if claim is not None and type(claim) is not bool:
            raise ValueError("invalid completion claim in raw evaluation record")
        if verified_claim is not None and type(verified_claim) is not bool:
            raise ValueError("invalid verified completion in raw evaluation record")
        run_status = record.get("run_status")
        if run_status is not None and (
            not isinstance(run_status, str) or run_status not in RUN_STATUS_VALUES
        ):
            raise ValueError("invalid Run status in raw evaluation record")
        if isinstance(oracle, list) and isinstance(evidence, list):
            if any(not isinstance(item, str) for item in (*oracle, *evidence)):
                raise ValueError("invalid oracle or accepted evidence in raw evaluation record")
            verified = record.get("outcome") == "succeeded" and set(oracle).issubset(evidence)
            if verified_claim is not verified:
                raise ValueError("verified completion disagrees with raw outcome and evidence")
        elif verified_claim is True:
            raise ValueError("verified completion lacks raw oracle or accepted evidence")
        source = record.get("model_usage_source")
        input_tokens = record.get("model_input_tokens")
        output_tokens = record.get("model_output_tokens")
        if source == "provider_reported":
            if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens)):
                raise ValueError("invalid provider token counts in raw evaluation record")
        elif source == "unavailable" or source is None:
            if input_tokens is not None or output_tokens is not None:
                raise ValueError("unavailable token usage cannot contain estimates")
        else:
            raise ValueError("invalid model usage source in raw evaluation record")

    def count(record: dict[str, object], field: str) -> int:
        value = record.get(field, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"invalid {field} in raw evaluation record")
        return value

    def percentile(values: list[float], fraction: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        position = (len(ordered) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 3)

    def metrics(rows: list[dict[str, object]]) -> dict[str, object]:
        def late_evidenced_claim(row: dict[str, object]) -> bool:
            oracle = row.get("oracle")
            evidence = row.get("accepted_evidence")
            return (
                bool(row.get("claimed_complete"))
                and row.get("outcome") == "timeout"
                and row.get("run_status") == "succeeded"
                and isinstance(oracle, list)
                and isinstance(evidence, list)
                and set(oracle).issubset(evidence)
            )

        denominator = len(rows)
        completed = sum(row.get("verified_completion") is True for row in rows)
        late_evidenced_count = sum(late_evidenced_claim(row) for row in rows)
        false_completion = sum(
            bool(row.get("claimed_complete"))
            and row.get("verified_completion") is not True
            and not late_evidenced_claim(row)
            for row in rows
        )
        token_records = [
            row for row in rows if row.get("model_usage_source") == "provider_reported"
        ]
        latencies: list[float] = []
        for row in rows:
            value = row.get("elapsed_ms")
            if value is None:
                continue
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError("invalid elapsed_ms in raw evaluation record")
            latencies.append(float(value))
        return {
            "denominator": denominator,
            "evidence_based_completion_count": completed,
            "evidence_based_completion_rate": completed / denominator,
            "false_completion_count": false_completion,
            "late_evidenced_claim_count": late_evidenced_count,
            "failed_count": sum(row["outcome"] == "failed" for row in rows),
            "timeout_count": sum(row["outcome"] == "timeout" for row in rows),
            "blocked_count": sum(row["outcome"] == "blocked" for row in rows),
            "unsafe_duplicate_effects": sum(count(row, "unsafe_duplicate_effects") for row in rows),
            "permission_violations": sum(count(row, "permission_violations") for row in rows),
            "provider_usage_sample_count": len(token_records),
            "provider_input_tokens": sum(count(row, "model_input_tokens") for row in token_records),
            "provider_output_tokens": sum(
                count(row, "model_output_tokens") for row in token_records
            ),
            "latency_sample_count": len(latencies),
            "latency_p50_ms": percentile(latencies, 0.5),
            "latency_p95_ms": percentile(latencies, 0.95),
        }

    def control_metadata_issues(rows: list[dict[str, object]]) -> list[str]:
        issues: list[str] = []
        if any(
            not isinstance(row.get("oracle"), list)
            or not row["oracle"]
            or not isinstance(row.get("accepted_evidence"), list)
            for row in rows
        ):
            issues.append("raw_evidence_missing_or_invalid")
        for field in (
            "model_revision",
            "model_settings_hash",
            "capability_revision",
            "policy_revision",
            "fixtures_hash",
            "environment_hash",
            "evidence_tier",
        ):
            values = [row.get(field) for row in rows]
            hashes = field in {"model_settings_hash", "environment_hash"}
            if (
                any(
                    not isinstance(value, str)
                    or not value
                    or (hashes and len(value) != 64)
                    or (field == "evidence_tier" and value not in {"controlled", "live_provider"})
                    for value in values
                )
                or len(set(values)) != 1
            ):
                issues.append(f"{field}_missing_or_mixed")
        if any(
            row.get("evidence_tier") == "live_provider" and row.get("environment_hash") == "0" * 64
            for row in rows
        ):
            issues.append("live_provider_environment_identity_missing")
        return issues

    by_arm: dict[str, dict[str, object]] = {}
    for surface, downstream in ARMS:
        arm_rows = [
            row
            for row in records
            if (row.get("model_surface"), row.get("downstream")) == (surface, downstream)
        ]
        if arm_rows:
            by_arm[f"{surface}|{downstream}"] = metrics(arm_rows)
    labeled_rows = [row for row in records if "model_surface" in row or "downstream" in row]
    if any((row.get("model_surface"), row.get("downstream")) not in ARMS for row in labeled_rows):
        raise ValueError("unknown or incomplete evaluation arm in raw records")
    ablation_rows = [
        row for row in records if "ablation_feature" in row or "feature_enabled" in row
    ]
    task_rows = [row for row in records if row.get("evaluation_kind") == "task_suite"]
    evidence_tiers = {row.get("evidence_tier") for row in records}
    result: dict[str, object] = {
        **metrics(records),
        "by_arm": by_arm,
        "evidence_tier": (
            next(iter(evidence_tiers))
            if len(evidence_tiers) == 1
            and next(iter(evidence_tiers)) in {"controlled", "live_provider"}
            else None
        ),
    }
    if labeled_rows:
        result["design_kind"] = "four_arm"
        arms_by_case: dict[str, Counter[tuple[object, object]]] = defaultdict(Counter)
        rows_by_case: dict[str, list[dict[str, object]]] = defaultdict(list)
        for row in labeled_rows:
            case_id = row.get("case_id")
            if not isinstance(case_id, str) or not case_id:
                raise ValueError("four-arm evaluation row lacks case_id")
            arms_by_case[case_id][(row["model_surface"], row["downstream"])] += 1
            rows_by_case[case_id].append(row)
        incomplete = sorted(
            case_id
            for case_id, counts in arms_by_case.items()
            if any(counts[arm] != 1 for arm in ARMS)
        )
        issues: list[str] = []
        issues.extend(control_metadata_issues(labeled_rows))
        if len(labeled_rows) != len(records):
            issues.append("mixed_four_arm_and_unlabeled_rows")
        hashes = [row.get("controls_hash") for row in labeled_rows]
        if (
            any(not isinstance(value, str) or len(value) != 64 for value in hashes)
            or len(set(hashes)) != 1
        ):
            issues.append("control_hash_missing_or_mixed")
        seeds = [row.get("arm_order_seed") for row in labeled_rows]
        if any(type(seed) is not int or seed < 0 for seed in seeds) or len(set(seeds)) != 1:
            issues.append("arm_order_seed_missing_or_mixed")
        case_indices: set[int] = set()
        for case_id, rows in rows_by_case.items():
            case_hashes = [row.get("case_hash") for row in rows]
            oracles = [row.get("oracle") for row in rows]
            indices = [row.get("case_index") for row in rows]
            if (
                any(not isinstance(value, str) or len(value) != 64 for value in case_hashes)
                or len(set(case_hashes)) != 1
                or any(not isinstance(value, list) or not value for value in oracles)
                or any(value != oracles[0] for value in oracles)
                or any(type(value) is not int or value < 0 for value in indices)
                or len(set(indices)) != 1
            ):
                issues.append(f"case_metadata_missing_or_mixed:{case_id}")
                continue
            case_index = indices[0]
            assert isinstance(case_index, int)
            if case_index in case_indices:
                issues.append(f"case_index_reused:{case_index}")
            case_indices.add(case_index)
            if len(set(seeds)) == 1 and type(seeds[0]) is int and seeds[0] >= 0:
                for row in rows:
                    arm_index = row.get("arm_index")
                    if (
                        type(arm_index) is not int
                        or arm_index not in range(len(ARMS))
                        or (row["model_surface"], row["downstream"])
                        != ARMS[(seeds[0] + case_index + arm_index) % len(ARMS)]
                    ):
                        issues.append(f"arm_position_invalid:{case_id}")
                        break
        if case_indices != set(range(len(rows_by_case))):
            issues.append("case_indices_not_contiguous")
        result.update(
            {
                "design_complete": not incomplete and not issues,
                "complete_case_count": len(arms_by_case) - len(incomplete),
                "incomplete_case_ids": incomplete,
                "design_issues": sorted(set(issues)),
                "duplicate_case_arm_rows": sum(
                    max(0, count - 1)
                    for counts in arms_by_case.values()
                    for count in counts.values()
                ),
            }
        )
    elif ablation_rows:
        result["design_kind"] = "ablation"
        if any(
            not isinstance(row.get("ablation_feature"), str)
            or row.get("ablation_feature")
            not in {"on_demand_catalog", "classifier", "skills", "child_runs"}
            or type(row.get("feature_enabled")) is not bool
            for row in ablation_rows
        ):
            raise ValueError("invalid ablation feature or condition in raw records")
        rows_by_case = defaultdict(list)
        for row in ablation_rows:
            case_id = row.get("case_id")
            if not isinstance(case_id, str) or not case_id:
                raise ValueError("ablation row lacks case_id")
            rows_by_case[case_id].append(row)
        incomplete = sorted(
            case_id
            for case_id, rows in rows_by_case.items()
            if Counter(row["feature_enabled"] for row in rows) != Counter({False: 1, True: 1})
        )
        issues = []
        issues.extend(control_metadata_issues(ablation_rows))
        if len(ablation_rows) != len(records):
            issues.append("mixed_ablation_and_unlabeled_rows")
        features = [row["ablation_feature"] for row in ablation_rows]
        if len(set(features)) != 1:
            issues.append("ablation_feature_mixed")
        hashes = [row.get("controls_hash") for row in ablation_rows]
        if (
            any(not isinstance(value, str) or len(value) != 64 for value in hashes)
            or len(set(hashes)) != 1
        ):
            issues.append("control_hash_missing_or_mixed")
        seeds = [row.get("arm_order_seed") for row in ablation_rows]
        if any(type(seed) is not int or seed < 0 for seed in seeds) or len(set(seeds)) != 1:
            issues.append("arm_order_seed_missing_or_mixed")
        case_indices = set()
        for case_id, rows in rows_by_case.items():
            case_hashes = [row.get("case_hash") for row in rows]
            oracles = [row.get("oracle") for row in rows]
            indices = [row.get("case_index") for row in rows]
            if (
                any(not isinstance(value, str) or len(value) != 64 for value in case_hashes)
                or len(set(case_hashes)) != 1
                or any(not isinstance(value, list) or not value for value in oracles)
                or any(value != oracles[0] for value in oracles)
                or any(type(value) is not int or value < 0 for value in indices)
                or len(set(indices)) != 1
            ):
                issues.append(f"case_metadata_missing_or_mixed:{case_id}")
                continue
            case_index = indices[0]
            assert isinstance(case_index, int)
            if case_index in case_indices:
                issues.append(f"case_index_reused:{case_index}")
            case_indices.add(case_index)
            if len(set(seeds)) == 1 and type(seeds[0]) is int and seeds[0] >= 0:
                for row in rows:
                    condition_index = row.get("condition_index")
                    if (
                        type(condition_index) is not int
                        or condition_index not in (0, 1)
                        or row["feature_enabled"]
                        is not bool((seeds[0] + case_index + condition_index) % 2)
                    ):
                        issues.append(f"condition_position_invalid:{case_id}")
                        break
        if case_indices != set(range(len(rows_by_case))):
            issues.append("case_indices_not_contiguous")
        result.update(
            {
                "design_complete": not incomplete and not issues,
                "complete_case_count": len(rows_by_case) - len(incomplete),
                "incomplete_case_ids": incomplete,
                "design_issues": sorted(set(issues)),
                "ablation_feature": features[0],
                "by_condition": {
                    "off": metrics(
                        [row for row in ablation_rows if row["feature_enabled"] is False]
                    ),
                    "on": metrics([row for row in ablation_rows if row["feature_enabled"] is True]),
                },
                "duplicate_condition_rows": sum(
                    max(0, count - 1)
                    for rows in rows_by_case.values()
                    for count in Counter(row["feature_enabled"] for row in rows).values()
                ),
            }
        )
    elif task_rows:
        result["design_kind"] = "task_suite"
        issues = []
        issues.extend(control_metadata_issues(task_rows))
        if len(task_rows) != len(records):
            issues.append("mixed_task_suite_and_other_rows")
        sizes = [row.get("suite_size") for row in task_rows]
        if any(type(value) is not int or value <= 0 for value in sizes) or len(set(sizes)) != 1:
            issues.append("suite_size_missing_or_mixed")
        suite_hashes = [row.get("suite_hash") for row in task_rows]
        if (
            any(not isinstance(value, str) or len(value) != 64 for value in suite_hashes)
            or len(set(suite_hashes)) != 1
        ):
            issues.append("suite_hash_missing_or_mixed")
        control_hashes = [row.get("controls_hash") for row in task_rows]
        if (
            any(not isinstance(value, str) or len(value) != 64 for value in control_hashes)
            or len(set(control_hashes)) != 1
        ):
            issues.append("control_hash_missing_or_mixed")
        case_ids = [row.get("case_id") for row in task_rows]
        if any(not isinstance(value, str) or not value for value in case_ids):
            raise ValueError("task suite row lacks case_id")
        if len(set(case_ids)) != len(case_ids):
            issues.append("duplicate_case_ids")
        indices = [row.get("case_index") for row in task_rows]
        if any(type(value) is not int or value < 0 for value in indices):
            issues.append("case_index_invalid")
        elif len(set(indices)) != len(indices):
            issues.append("duplicate_case_indices")
        if any(
            not isinstance(case_hash := row.get("case_hash"), str)
            or len(case_hash) != 64
            or not isinstance(oracle := row.get("oracle"), list)
            or not oracle
            for row in task_rows
        ):
            issues.append("case_metadata_missing")
        missing_indices: list[int] = []
        if "suite_size_missing_or_mixed" not in issues and "case_index_invalid" not in issues:
            suite_size = sizes[0]
            assert isinstance(suite_size, int)
            valid_indices = [cast(int, index) for index in indices]
            missing_indices = sorted(set(range(suite_size)) - set(valid_indices))
            if missing_indices or any(index >= suite_size for index in valid_indices):
                issues.append("case_indices_not_complete")
        result.update(
            {
                "design_complete": not issues,
                "complete_case_count": len(set(case_ids)),
                "missing_case_indices": missing_indices,
                "design_issues": sorted(set(issues)),
                "by_case": {
                    case_id: metrics([row for row in task_rows if row["case_id"] == case_id])
                    for case_id in case_ids
                    if isinstance(case_id, str)
                },
            }
        )
    else:
        result["design_kind"] = None
        result["design_complete"] = None
    return result
