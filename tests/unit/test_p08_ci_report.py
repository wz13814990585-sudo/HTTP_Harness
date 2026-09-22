"""The CI evidence gate must fail on silent skips and missing AT tests."""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml

from scripts.check_ci_test_report import check_report, main


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    suite = ElementTree.Element("testsuite", tests="4", skipped="2")
    ElementTree.SubElement(
        suite,
        "testcase",
        classname="tests.integration.test_example",
        name="test_passed",
    )
    ElementTree.SubElement(
        suite,
        "testcase",
        classname="tests.integration.test_example",
        name="test_partial",
    )
    live = ElementTree.SubElement(
        suite,
        "testcase",
        classname="tests.integration.test_example",
        name="test_live",
    )
    ElementTree.SubElement(live, "skipped", message="external credential absent")
    optional_live = ElementTree.SubElement(
        suite,
        "testcase",
        classname="tests.integration.test_example",
        name="test_optional_live",
    )
    ElementTree.SubElement(optional_live, "skipped", message="external credential absent")
    junit = tmp_path / "junit.xml"
    ElementTree.ElementTree(suite).write(junit, encoding="unicode")
    acceptance = tmp_path / "acceptance.json"
    acceptance.write_text(
        json.dumps(
            {
                "summary": {
                    "passed": 1,
                    "blocked_environment": 1,
                    "failed": 0,
                    "not_run": 0,
                    "total": 2,
                },
                "cases": [
                    {
                        "id": "AT-pass",
                        "status": "passed",
                        "tests": ["tests/integration/test_example.py::test_passed"],
                        "live_tests": [
                            "tests/integration/test_example.py::test_optional_live"
                        ],
                    },
                    {
                        "id": "AT-blocked",
                        "status": "blocked_environment",
                        "tests": ["tests/integration/test_example.py::test_live"],
                        "partial_tests": ["tests/integration/test_example.py::test_partial"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return junit, acceptance


def _spec(tmp_path: Path, ids: list[str]) -> Path:
    path = tmp_path / "acceptance-spec.yaml"
    path.write_text(yaml.safe_dump({"cases": [{"id": item} for item in ids]}))
    return path


def test_ci_report_accepts_only_declared_live_skip_and_executed_partial_evidence(
    tmp_path: Path,
) -> None:
    junit, acceptance = _inputs(tmp_path)
    result = check_report(junit, acceptance)
    assert result["evidence_consistent"] is True
    assert result["release_ready"] is False
    assert result["blocked_acceptance_ids"] == ["AT-blocked"]
    assert result["collected_testcases"] == 4
    assert result["mapped_executed_testcases"] == 2
    assert result["observed_skips"] == result["expected_skips"] == 2


def test_ci_report_rejects_missing_passed_test(tmp_path: Path) -> None:
    junit, acceptance = _inputs(tmp_path)
    source = json.loads(acceptance.read_text())
    source["cases"][0]["tests"] = ["tests/integration/test_example.py::test_not_collected"]
    acceptance.write_text(json.dumps(source))
    result = check_report(junit, acceptance)
    assert result["evidence_consistent"] is False
    assert result["release_ready"] is False
    assert any("mapped_test_not_collected" in item for item in result["issues"])


def test_ci_report_rejects_unexpected_skip_and_stale_summary(tmp_path: Path) -> None:
    junit, acceptance = _inputs(tmp_path)
    tree = ElementTree.parse(junit)
    first = tree.getroot().find("testcase")
    assert first is not None
    ElementTree.SubElement(first, "skipped", message="unexpected")
    tree.write(junit, encoding="unicode")
    source = json.loads(acceptance.read_text())
    source["summary"]["passed"] = 2
    acceptance.write_text(json.dumps(source))
    result = check_report(junit, acceptance)
    assert result["evidence_consistent"] is False
    assert result["release_ready"] is False
    assert "acceptance_summary_mismatch" in result["issues"]
    assert any("mapped_test_skipped" in item for item in result["issues"])
    assert any("unexpected_skip" in item for item in result["issues"])


def test_ci_report_rejects_missing_or_failed_optional_live_test(tmp_path: Path) -> None:
    junit, acceptance = _inputs(tmp_path)
    source = json.loads(acceptance.read_text())
    source["cases"][0]["live_tests"] = [
        "tests/integration/test_example.py::test_not_collected"
    ]
    acceptance.write_text(json.dumps(source))
    result = check_report(junit, acceptance)
    assert result["evidence_consistent"] is False
    assert any("live_test_not_collected" in item for item in result["issues"])

    source["cases"][0]["live_tests"] = [
        "tests/integration/test_example.py::test_optional_live"
    ]
    acceptance.write_text(json.dumps(source))
    tree = ElementTree.parse(junit)
    live = tree.getroot().find("testcase[@name='test_optional_live']")
    assert live is not None
    skipped = live.find("skipped")
    assert skipped is not None
    live.remove(skipped)
    ElementTree.SubElement(live, "failure", message="provider failed")
    tree.write(junit, encoding="unicode")
    result = check_report(junit, acceptance)
    assert result["evidence_consistent"] is False
    assert any("live_test_failed" in item for item in result["issues"])


def test_release_gate_rejects_declared_blockers_even_when_ci_mapping_is_consistent(
    tmp_path: Path,
) -> None:
    junit, acceptance = _inputs(tmp_path)
    spec = _spec(tmp_path, ["AT-pass", "AT-blocked"])
    assert main([str(junit), str(acceptance), "--spec", str(spec)]) == 0
    assert main([str(junit), str(acceptance), "--spec", str(spec), "--require-release-ready"]) == 2


def test_release_gate_passes_only_all_executed_acceptance(tmp_path: Path) -> None:
    junit, acceptance = _inputs(tmp_path)
    tree = ElementTree.parse(junit)
    live = tree.getroot().find("testcase[@name='test_live']")
    assert live is not None
    tree.getroot().remove(live)
    tree.write(junit, encoding="unicode")
    status = json.loads(acceptance.read_text())
    status["summary"] = {
        "passed": 2,
        "blocked_environment": 0,
        "failed": 0,
        "not_run": 0,
        "total": 2,
    }
    status["cases"][1] = {
        "id": "AT-blocked",
        "status": "passed",
        "tests": ["tests/integration/test_example.py::test_partial"],
    }
    acceptance.write_text(json.dumps(status))
    spec = _spec(tmp_path, ["AT-pass", "AT-blocked"])
    result = check_report(junit, acceptance, spec)
    assert result["evidence_consistent"] is True
    assert result["release_ready"] is True
    assert result["spec_coverage_checked"] is True
    assert result["blocked_acceptance_ids"] == []
    assert main([str(junit), str(acceptance), "--spec", str(spec), "--require-release-ready"]) == 0


def test_release_gate_rejects_missing_or_duplicate_spec_ids(tmp_path: Path) -> None:
    junit, acceptance = _inputs(tmp_path)
    missing = _spec(tmp_path, ["AT-pass", "AT-blocked", "AT-omitted"])
    result = check_report(junit, acceptance, missing)
    assert result["evidence_consistent"] is False
    assert "acceptance_spec_mismatch" in result["issues"]

    source = json.loads(acceptance.read_text())
    source["cases"][1]["id"] = "AT-pass"
    acceptance.write_text(json.dumps(source))
    result = check_report(junit, acceptance, missing)
    assert result["evidence_consistent"] is False
    assert "duplicate_acceptance_id" in result["issues"]


def test_ci_gate_reports_malformed_spec_as_failed_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    junit, acceptance = _inputs(tmp_path)
    spec = tmp_path / "broken.yaml"
    spec.write_text("cases: [")
    assert main([str(junit), str(acceptance), "--spec", str(spec)]) == 2
    # Keep the check machine-readable and avoid a traceback during CI.
    output = json.loads(capsys.readouterr().out)
    assert output["evidence_consistent"] is False
    assert output["release_ready"] is False
