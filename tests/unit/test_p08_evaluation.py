from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hnh.evaluation import (
    ARMS,
    AblationRunner,
    EvaluationCase,
    EvaluationControls,
    EvaluationDesignError,
    EvaluationResult,
    ExperimentRunner,
    TaskSuiteRunner,
    summarize,
)


class ControlledExecutor:
    def __init__(self) -> None:
        self.controls_seen: list[EvaluationControls] = []

    def run(
        self,
        case: EvaluationCase,
        controls: EvaluationControls,
        surface: str,
        downstream: str,
    ) -> EvaluationResult:
        self.controls_seen.append(controls)
        if (surface, downstream) == ARMS[1]:
            raise TimeoutError()
        if (surface, downstream) == ARMS[2]:
            return EvaluationResult(
                "run-failed",
                "failed",
                (),
                True,
                None,
                None,
                "unavailable",
                failure_reason="bad parameters",
            )
        if (surface, downstream) == ARMS[3]:
            return EvaluationResult(
                "run-blocked",
                "blocked",
                (),
                False,
                None,
                None,
                "unavailable",
                failure_reason="missing remote integration",
            )
        return EvaluationResult(
            "run-ok", "succeeded", ("event-1",), True, 100, 20, "provider_reported"
        )


def test_at_065_four_arm_runner_uses_identical_controls_and_retains_raw_failures(
    tmp_path: Path,
) -> None:
    controls = EvaluationControls(
        "pinned-model",
        {"temperature": 0},
        "cap-1",
        "policy-1",
        ("workspace:read",),
        {"max_model_turns": 2},
        30.0,
        "fixture-sha256",
    )
    executor = ControlledExecutor()
    output = tmp_path / "raw.jsonl"
    records = ExperimentRunner(executor, output).run(
        (EvaluationCase("case-1", "read document", ("event-1",)),), controls
    )
    assert len(records) == 4
    assert set((row["model_surface"], row["downstream"]) for row in records) == set(ARMS)
    assert len({row["controls_hash"] for row in records}) == 1
    assert executor.controls_seen == [controls] * 4
    assert len(output.read_text().splitlines()) == 4
    assert json.loads(output.read_text().splitlines()[1])["outcome"] == "timeout"
    with pytest.raises(FileExistsError):
        ExperimentRunner(executor, output).run(
            (EvaluationCase("case-1", "read document", ("event-1",)),), controls
        )


def test_at_068_summary_counts_timeout_blocked_failure_and_no_fake_token_usage(
    tmp_path: Path,
) -> None:
    controls = EvaluationControls(
        "pinned-model",
        {},
        "cap-1",
        "policy-1",
        (),
        {"max_model_turns": 2},
        30.0,
        "fixture-sha256",
    )
    records = ExperimentRunner(ControlledExecutor(), tmp_path / "raw.jsonl").run(
        (EvaluationCase("case-1", "read document", ("event-1",)),), controls
    )
    summary = summarize(records)
    assert summary["denominator"] == 4
    assert summary["evidence_based_completion_count"] == 1
    assert summary["evidence_based_completion_rate"] == 0.25
    assert summary["false_completion_count"] == 1
    assert summary["failed_count"] == 1
    assert summary["timeout_count"] == 1
    assert summary["blocked_count"] == 1
    assert summary["provider_usage_sample_count"] == 1
    assert summary["provider_input_tokens"] == 100
    assert summary["provider_output_tokens"] == 20


def test_timeout_with_evidence_separates_late_success_from_false_claim() -> None:
    row: dict[str, object] = {
        "outcome": "timeout",
        "oracle": ["proof"],
        "accepted_evidence": ["proof"],
        "claimed_complete": True,
        "verified_completion": False,
        "model_usage_source": "unavailable",
        "model_input_tokens": None,
        "model_output_tokens": None,
    }
    summary = summarize([{**row, "run_status": "succeeded"}, {**row, "run_status": "blocked"}])
    assert summary["denominator"] == 2
    assert summary["timeout_count"] == 2
    assert summary["late_evidenced_claim_count"] == 1
    assert summary["false_completion_count"] == 1
    assert summary["evidence_based_completion_count"] == 0
    with pytest.raises(ValueError, match="Run status"):
        summarize([{**row, "run_status": "not-a-state"}])


def test_usage_does_not_accept_estimated_tokens() -> None:
    with pytest.raises(ValueError):
        EvaluationResult("run", "succeeded", (), True, 100, 20, "unavailable")
    with pytest.raises(ValueError, match="nonnegative integers"):
        EvaluationResult("run", "succeeded", (), True, True, 20, "provider_reported")
    with pytest.raises(ValueError, match="nonnegative integers"):
        EvaluationResult(
            "run", "failed", (), False, None, None, "unavailable", permission_violations=True
        )


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), True])
def test_evaluation_controls_require_finite_positive_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        EvaluationControls("m", {}, "c", "p", (), {}, timeout, "fixture")


def test_evaluation_controls_require_a_sha256_environment_hash() -> None:
    with pytest.raises(ValueError, match="environment hash"):
        EvaluationControls(
            "m",
            {},
            "c",
            "p",
            (),
            {},
            1.0,
            "fixture",
            environment_hash="not-a-hash",
        )

    with pytest.raises(ValueError, match="evidence tier"):
        EvaluationControls(
            "m",
            {},
            "c",
            "p",
            (),
            {},
            1.0,
            "fixture",
            evidence_tier="synthetic",  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="environment identity"):
        EvaluationControls(
            "m",
            {},
            "c",
            "p",
            (),
            {},
            1.0,
            "fixture",
            evidence_tier="live_provider",
        )


def test_four_arm_order_is_reproducibly_counterbalanced_and_reported_by_arm(
    tmp_path: Path,
) -> None:
    controls = EvaluationControls(
        "pinned-model",
        {},
        "cap-1",
        "policy-1",
        (),
        {"max_model_turns": 2},
        30.0,
        "fixture-sha256",
        arm_order_seed=1,
    )
    cases = tuple(
        EvaluationCase(f"case-{index}", "read document", ("event-1",)) for index in range(4)
    )
    records = ExperimentRunner(ControlledExecutor(), tmp_path / "balanced.jsonl").run(
        cases, controls
    )
    assert len(records) == 16
    for case_index in range(4):
        case_rows = [row for row in records if row["case_index"] == case_index]
        assert [row["arm_index"] for row in case_rows] == [0, 1, 2, 3]
        assert [(row["model_surface"], row["downstream"]) for row in case_rows] == list(
            ARMS[(case_index + 1) % 4 :] + ARMS[: (case_index + 1) % 4]
        )
    for arm_index in range(4):
        assert {
            (row["model_surface"], row["downstream"])
            for row in records
            if row["arm_index"] == arm_index
        } == set(ARMS)
    summary = summarize(records)
    by_arm = summary["by_arm"]
    assert isinstance(by_arm, dict)
    assert len(by_arm) == 4
    assert all(group["denominator"] == 4 for group in by_arm.values())
    assert by_arm["native_function_schema|native_http"]["evidence_based_completion_count"] == 4
    assert by_arm["native_function_schema|mcp_adapter"]["timeout_count"] == 4
    assert summary["denominator"] == 16
    assert summary["design_complete"] is True
    assert summary["complete_case_count"] == 4
    assert summary["incomplete_case_ids"] == []
    assert summary["latency_sample_count"] == 16
    assert summary["latency_p95_ms"] is not None


def test_four_arm_runner_stops_if_executor_mutates_shared_controls(tmp_path: Path) -> None:
    class MutatingExecutor(ControlledExecutor):
        def run(
            self, case: EvaluationCase, controls: EvaluationControls, surface: str, downstream: str
        ) -> EvaluationResult:
            controls.budget["max_model_turns"] = 999
            return super().run(case, controls, surface, downstream)

    controls = EvaluationControls("m", {}, "c", "p", (), {"max_model_turns": 2}, 1.0, "fixture")
    with pytest.raises(ValueError, match="controls mutated"):
        ExperimentRunner(MutatingExecutor(), tmp_path / "raw.jsonl").run(
            (EvaluationCase("case-1", "task", ("event-1",)),), controls
        )
    assert (tmp_path / "raw.jsonl").read_text() == ""


def test_four_arm_runner_stops_on_capability_design_drift(tmp_path: Path) -> None:
    class DriftingExecutor(ControlledExecutor):
        def run(
            self, case: EvaluationCase, controls: EvaluationControls, surface: str, downstream: str
        ) -> EvaluationResult:
            raise EvaluationDesignError("remote capability changed after preflight")

    output = tmp_path / "raw.jsonl"
    controls = EvaluationControls("m", {}, "c", "p", (), {}, 1.0, "fixture")
    with pytest.raises(EvaluationDesignError, match="changed after preflight"):
        ExperimentRunner(DriftingExecutor(), output).run(
            (EvaluationCase("case-1", "task", ("event-1",)),), controls
        )
    assert output.read_text() == ""


def test_evaluation_summary_rejects_unknown_arm_and_invalid_latency() -> None:
    base: dict[str, object] = {
        "case_id": "case-1",
        "outcome": "failed",
        "verified_completion": False,
        "claimed_complete": False,
        "model_usage_source": "unavailable",
        "elapsed_ms": 1.0,
        "model_surface": "native_function_schema",
        "downstream": "native_http",
    }
    with pytest.raises(ValueError, match="unknown or incomplete evaluation arm"):
        summarize([{**base, "downstream": "not-an-arm"}])
    with pytest.raises(ValueError, match="invalid elapsed_ms"):
        summarize([{**base, "elapsed_ms": float("nan")}])
    with pytest.raises(ValueError, match="arm order seed"):
        EvaluationControls("m", {}, "c", "p", (), {}, 1.0, "fixture", arm_order_seed=-1)


def test_partial_or_duplicate_four_arm_raw_file_is_explicitly_incomplete() -> None:
    row: dict[str, object] = {
        "case_id": "case-1",
        "outcome": "succeeded",
        "verified_completion": True,
        "claimed_complete": True,
        "model_usage_source": "unavailable",
        "oracle": ["event-1"],
        "accepted_evidence": ["event-1"],
        "model_surface": "native_function_schema",
        "downstream": "native_http",
    }
    partial = summarize([row])
    assert partial["denominator"] == 1
    assert partial["design_complete"] is False
    assert partial["incomplete_case_ids"] == ["case-1"]
    duplicated = summarize([row, row])
    assert duplicated["design_complete"] is False
    assert duplicated["duplicate_case_arm_rows"] == 1


def test_complete_arm_set_with_mixed_controls_or_oracles_is_not_valid(
    tmp_path: Path,
) -> None:
    controls = EvaluationControls(
        "pinned-model", {}, "cap-1", "policy-1", (), {"max_model_turns": 2}, 30.0, "fixture"
    )
    rows = ExperimentRunner(ControlledExecutor(), tmp_path / "raw.jsonl").run(
        (EvaluationCase("case-1", "read document", ("event-1",)),), controls
    )
    assert summarize(rows)["design_complete"] is True

    mixed_controls = [dict(row) for row in rows]
    mixed_controls[3]["controls_hash"] = "f" * 64
    result = summarize(mixed_controls)
    assert result["incomplete_case_ids"] == []
    assert result["design_complete"] is False
    assert "control_hash_missing_or_mixed" in result["design_issues"]

    mixed_revision = [dict(row) for row in rows]
    mixed_revision[0]["model_revision"] = "different-model"
    result = summarize(mixed_revision)
    assert result["design_complete"] is False
    assert "model_revision_missing_or_mixed" in result["design_issues"]

    missing_settings_hash = [dict(row) for row in rows]
    del missing_settings_hash[0]["model_settings_hash"]
    assert (
        "model_settings_hash_missing_or_mixed" in summarize(missing_settings_hash)["design_issues"]
    )

    mixed_environment = [dict(row) for row in rows]
    mixed_environment[0]["environment_hash"] = "f" * 64
    assert "environment_hash_missing_or_mixed" in summarize(mixed_environment)["design_issues"]

    mixed_tier = [dict(row) for row in rows]
    mixed_tier[0]["evidence_tier"] = "live_provider"
    assert "evidence_tier_missing_or_mixed" in summarize(mixed_tier)["design_issues"]

    falsely_live = [{**row, "evidence_tier": "live_provider"} for row in rows]
    assert "live_provider_environment_identity_missing" in summarize(falsely_live)["design_issues"]

    mixed_oracle = [dict(row) for row in rows]
    mixed_oracle[3]["oracle"] = ["different-evidence"]
    assert "case_metadata_missing_or_mixed:case-1" in summarize(mixed_oracle)["design_issues"]

    bad_position = [dict(row) for row in rows]
    bad_position[3]["arm_index"] = 1
    assert "arm_position_invalid:case-1" in summarize(bad_position)["design_issues"]

    unlabeled = dict(rows[0])
    del unlabeled["model_surface"]
    del unlabeled["downstream"]
    mixed_rows = summarize([*rows, unlabeled])
    assert mixed_rows["design_complete"] is False
    assert "mixed_four_arm_and_unlabeled_rows" in mixed_rows["design_issues"]


def test_summary_refuses_inflated_completion_claim_in_raw_record(tmp_path: Path) -> None:
    controls = EvaluationControls("m", {}, "c", "p", (), {}, 1.0, "fixture")
    rows = ExperimentRunner(ControlledExecutor(), tmp_path / "raw.jsonl").run(
        (EvaluationCase("case-1", "task", ("event-1",)),), controls
    )
    tampered = [dict(row) for row in rows]
    tampered[1]["verified_completion"] = True
    with pytest.raises(ValueError, match="disagrees with raw outcome and evidence"):
        summarize(tampered)

    missing_evidence = [dict(row) for row in rows]
    del missing_evidence[0]["accepted_evidence"]
    with pytest.raises(ValueError, match="lacks raw oracle or accepted evidence"):
        summarize(missing_evidence)


def test_summary_rejects_invalid_provider_usage_in_complete_dataset(tmp_path: Path) -> None:
    controls = EvaluationControls("m", {}, "c", "p", (), {}, 1.0, "fixture")
    rows = ExperimentRunner(ControlledExecutor(), tmp_path / "raw.jsonl").run(
        (EvaluationCase("case-1", "task", ("event-1",)),), controls
    )
    assert summarize(rows)["design_complete"] is True

    with_estimate = [dict(row) for row in rows]
    with_estimate[1]["model_input_tokens"] = 42
    with pytest.raises(ValueError, match="cannot contain estimates"):
        summarize(with_estimate)

    bogus_source = [dict(row) for row in rows]
    bogus_source[1]["model_usage_source"] = "estimated"
    with pytest.raises(ValueError, match="invalid model usage source"):
        summarize(bogus_source)

    boolean_count = [dict(row) for row in rows]
    boolean_count[0]["model_input_tokens"] = True
    with pytest.raises(ValueError, match="invalid provider token counts"):
        summarize(boolean_count)

    missing_failed_evidence = [dict(row) for row in rows]
    del missing_failed_evidence[1]["accepted_evidence"]
    summary = summarize(missing_failed_evidence)
    assert summary["design_complete"] is False
    assert "raw_evidence_missing_or_invalid" in summary["design_issues"]


def test_summary_cli_rejects_complete_arms_with_mixed_controls(tmp_path: Path) -> None:
    controls = EvaluationControls("m", {}, "c", "p", (), {}, 1.0, "fixture")
    raw = tmp_path / "raw.jsonl"
    rows = ExperimentRunner(ControlledExecutor(), raw).run(
        (EvaluationCase("case-1", "task", ("event-1",)),), controls
    )
    rows[0]["controls_hash"] = "0" * 64
    raw.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    result = subprocess.run(
        [sys.executable, "eval/summarize.py", str(raw)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "control_hash_missing_or_mixed" in json.loads(result.stdout)["design_issues"]


def test_summary_cli_rejects_mixed_model_revision_despite_same_controls_hash(
    tmp_path: Path,
) -> None:
    controls = EvaluationControls("m", {}, "c", "p", (), {}, 1.0, "fixture")
    raw = tmp_path / "mixed-model.raw.jsonl"
    rows = ExperimentRunner(ControlledExecutor(), raw).run(
        (EvaluationCase("case-1", "task", ("event-1",)),), controls
    )
    rows[0]["model_revision"] = "other-model"
    raw.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    result = subprocess.run(
        [sys.executable, "eval/summarize.py", str(raw)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "model_revision_missing_or_mixed" in json.loads(result.stdout)["design_issues"]


def test_summary_cli_exits_nonzero_for_truncated_four_arm_run(tmp_path: Path) -> None:
    raw = tmp_path / "truncated.jsonl"
    raw.write_text(
        json.dumps(
            {
                "case_id": "case-1",
                "outcome": "failed",
                "verified_completion": False,
                "claimed_complete": False,
                "model_usage_source": "unavailable",
                "model_surface": "native_function_schema",
                "downstream": "native_http",
            }
        )
        + "\n"
    )
    result = subprocess.run(
        [sys.executable, "eval/summarize.py", str(raw)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["incomplete_case_ids"] == ["case-1"]


def test_summary_cli_rejects_unlabeled_raw_rows(tmp_path: Path) -> None:
    raw = tmp_path / "unlabeled.jsonl"
    raw.write_text('{"outcome":"failed","verified_completion":false}\n')
    result = subprocess.run(
        [sys.executable, "eval/summarize.py", str(raw)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["design_complete"] is None


class ControlledAblation:
    def __init__(self) -> None:
        self.flags: list[bool] = []

    def run(
        self, case: EvaluationCase, controls: EvaluationControls, feature: str, enabled: bool
    ) -> EvaluationResult:
        self.flags.append(enabled)
        assert feature == "classifier"
        return EvaluationResult(
            f"run-{enabled}",
            "succeeded",
            ("event-1",),
            True,
            100,
            20,
            "provider_reported",
        )


def test_ablation_is_independent_off_on_and_has_own_raw_file(tmp_path: Path) -> None:
    controls = EvaluationControls(
        "pinned-model",
        {},
        "cap-1",
        "policy-1",
        (),
        {"max_model_turns": 2},
        30.0,
        "fixture-sha256",
    )
    executor = ControlledAblation()
    records = AblationRunner(executor, tmp_path / "classifier.raw.jsonl").run(
        (EvaluationCase("case-1", "read document", ("event-1",)),), controls, "classifier"
    )
    assert executor.flags == [False, True]
    assert [record["feature_enabled"] for record in records] == [False, True]
    assert len({record["controls_hash"] for record in records}) == 1
    summary = summarize(records)
    assert summary["design_kind"] == "ablation"
    assert summary["design_complete"] is True
    assert summary["by_condition"]["off"]["denominator"] == 1
    assert summary["by_condition"]["on"]["denominator"] == 1


def test_ablation_balances_order_and_rejects_partial_or_mixed_records(tmp_path: Path) -> None:
    controls = EvaluationControls(
        "pinned-model", {}, "cap-1", "policy-1", (), {}, 30.0, "fixture", arm_order_seed=1
    )
    cases = tuple(EvaluationCase(f"case-{index}", "task", ("event-1",)) for index in range(4))
    raw = tmp_path / "ablation.jsonl"
    records = AblationRunner(ControlledAblation(), raw).run(cases, controls, "classifier")
    assert len(records) == 8
    assert [row["feature_enabled"] for row in records] == [
        True,
        False,
        False,
        True,
        True,
        False,
        False,
        True,
    ]
    summary = summarize(records)
    assert summary["design_complete"] is True
    assert summary["complete_case_count"] == 4
    assert summary["by_condition"]["off"]["denominator"] == 4
    assert summary["by_condition"]["on"]["denominator"] == 4

    partial = summarize(records[:-1])
    assert partial["design_complete"] is False
    assert partial["incomplete_case_ids"] == ["case-3"]
    raw.write_text("\n".join(json.dumps(row) for row in records[:-1]) + "\n")
    cli = subprocess.run(
        [sys.executable, "eval/summarize.py", str(raw)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert cli.returncode == 2

    mixed = [dict(row) for row in records]
    mixed[0]["ablation_feature"] = "skills"
    assert "ablation_feature_mixed" in summarize(mixed)["design_issues"]
    mixed[0] = dict(records[0])
    mixed[0]["controls_hash"] = "f" * 64
    assert "control_hash_missing_or_mixed" in summarize(mixed)["design_issues"]
    mixed[0] = dict(records[0])
    mixed[0]["policy_revision"] = "different-policy"
    assert "policy_revision_missing_or_mixed" in summarize(mixed)["design_issues"]
    mixed[0] = dict(records[0])
    mixed[0]["condition_index"] = 1
    assert "condition_position_invalid:case-0" in summarize(mixed)["design_issues"]


def test_ablation_runner_stops_if_executor_mutates_shared_controls(tmp_path: Path) -> None:
    class MutatingAblation(ControlledAblation):
        def run(
            self, case: EvaluationCase, controls: EvaluationControls, feature: str, enabled: bool
        ) -> EvaluationResult:
            controls.model_settings["temperature"] = 1
            return super().run(case, controls, feature, enabled)

    controls = EvaluationControls("m", {}, "c", "p", (), {}, 1.0, "fixture")
    with pytest.raises(ValueError, match="controls mutated"):
        AblationRunner(MutatingAblation(), tmp_path / "raw.jsonl").run(
            (EvaluationCase("case-1", "task", ("event-1",)),), controls, "classifier"
        )
    assert (tmp_path / "raw.jsonl").read_text() == ""


class ControlledTaskSuite:
    def run(self, case: EvaluationCase, controls: EvaluationControls) -> EvaluationResult:
        assert controls.model == "pinned-model"
        if case.case_id == "task-1":
            raise TimeoutError()
        return EvaluationResult(
            f"run-{case.case_id}",
            "succeeded",
            case.expected_evidence,
            True,
            7,
            3,
            "provider_reported",
        )


def test_task_suite_retains_timeouts_and_requires_all_predeclared_cases(tmp_path: Path) -> None:
    controls = EvaluationControls("pinned-model", {}, "cap", "policy", (), {}, 5.0, "fixture")
    cases = tuple(
        EvaluationCase(f"task-{index}", f"goal {index}", (f"artifact:{index}",))
        for index in range(3)
    )
    raw = tmp_path / "tasks.jsonl"
    records = TaskSuiteRunner(ControlledTaskSuite(), raw).run(cases, controls)
    assert len(records) == 3
    assert {row["model_revision"] for row in records} == {"pinned-model"}
    assert {row["capability_revision"] for row in records} == {"cap"}
    assert {row["policy_revision"] for row in records} == {"policy"}
    assert len({row["model_settings_hash"] for row in records}) == 1
    assert {row["environment_hash"] for row in records} == {"0" * 64}
    assert [row["outcome"] for row in records] == ["succeeded", "timeout", "succeeded"]
    summary = summarize(records)
    assert summary["design_kind"] == "task_suite"
    assert summary["evidence_tier"] == "controlled"
    assert summary["design_complete"] is True
    assert summary["denominator"] == 3
    assert summary["evidence_based_completion_count"] == 2
    assert summary["timeout_count"] == 1
    assert summary["provider_usage_sample_count"] == 2
    assert summary["by_case"]["task-1"]["timeout_count"] == 1
    assert len(raw.read_text().splitlines()) == 3

    partial = summarize(records[:-1])
    assert partial["design_complete"] is False
    assert partial["missing_case_indices"] == [2]
    raw.write_text("\n".join(json.dumps(row) for row in records[:-1]) + "\n")
    cli = subprocess.run(
        [sys.executable, "eval/summarize.py", str(raw)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert cli.returncode == 2

    mixed = [dict(row) for row in records]
    mixed[1]["controls_hash"] = "f" * 64
    assert "control_hash_missing_or_mixed" in summarize(mixed)["design_issues"]
    mixed[1] = dict(records[1])
    mixed[1]["capability_revision"] = "other-capability"
    assert "capability_revision_missing_or_mixed" in summarize(mixed)["design_issues"]
    mixed[1] = dict(records[1])
    mixed[1]["fixtures_hash"] = "different-fixtures"
    assert "fixtures_hash_missing_or_mixed" in summarize(mixed)["design_issues"]
    assert "duplicate_case_ids" in summarize([*records, records[0]])["design_issues"]


def test_task_suite_runner_rejects_control_mutation(tmp_path: Path) -> None:
    class MutatingTaskSuite(ControlledTaskSuite):
        def run(self, case: EvaluationCase, controls: EvaluationControls) -> EvaluationResult:
            controls.budget["max_model_turns"] = 999
            return super().run(case, controls)

    controls = EvaluationControls("pinned-model", {}, "cap", "policy", (), {}, 5.0, "fixture")
    with pytest.raises(ValueError, match="controls mutated"):
        TaskSuiteRunner(MutatingTaskSuite(), tmp_path / "raw.jsonl").run(
            (EvaluationCase("case-1", "goal", ("artifact:one",)),), controls
        )
    assert (tmp_path / "raw.jsonl").read_text() == ""
