"""Fail CI when the runtime acceptance map and collected JUnit tests disagree."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

import yaml


def _test_key(reference: str) -> tuple[str, str]:
    source, separator, function = reference.partition("::")
    if (
        not separator
        or not source.startswith("tests/")
        or not source.endswith(".py")
        or not function
    ):
        raise ValueError(f"invalid acceptance test reference: {reference}")
    return source[:-3].replace("/", "."), function


def check_report(
    junit_path: Path, acceptance_path: Path, spec_path: Path | None = None
) -> dict[str, object]:
    root = ElementTree.parse(junit_path).getroot()
    testcases = root.findall(".//testcase")
    if not testcases:
        raise ValueError("JUnit report has no testcases")
    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    cases = acceptance.get("cases")
    summary = acceptance.get("summary")
    if not isinstance(cases, list) or not isinstance(summary, dict):
        raise ValueError("acceptance status has no cases or summary")
    if any(not isinstance(case, dict) for case in cases):
        raise ValueError("acceptance status cases must be objects")
    counts = Counter(case.get("status") for case in cases)
    issues: list[str] = []
    recorded_ids = [case.get("id") for case in cases]
    if any(not isinstance(item, str) for item in recorded_ids):
        issues.append("invalid_acceptance_id")
    recorded_ids = [item for item in recorded_ids if isinstance(item, str)]
    if len(recorded_ids) != len(set(recorded_ids)):
        issues.append("duplicate_acceptance_id")
    if spec_path is not None:
        specification = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        if not isinstance(specification, dict) or not isinstance(specification.get("cases"), list):
            raise ValueError("acceptance specification has no cases")
        if any(not isinstance(item, dict) for item in specification["cases"]):
            raise ValueError("acceptance specification cases must be objects")
        required_ids = [item.get("id") for item in specification["cases"]]
        if any(not isinstance(item, str) for item in required_ids):
            issues.append("invalid_acceptance_spec_ids")
        required_ids = [item for item in required_ids if isinstance(item, str)]
        if not required_ids or len(required_ids) != len(set(required_ids)):
            issues.append("invalid_acceptance_spec_ids")
        if set(recorded_ids) != set(required_ids):
            issues.append("acceptance_spec_mismatch")
    if summary.get("total") != len(cases) or any(
        summary.get(status) != counts[status]
        for status in ("passed", "failed", "blocked_environment", "not_run")
    ):
        issues.append("acceptance_summary_mismatch")
    if (
        counts["failed"]
        or counts["not_run"]
        or any(status not in {"passed", "blocked_environment"} for status in counts)
    ):
        issues.append("acceptance_status_not_ready_for_ci")

    indexed: dict[tuple[str, str], list[ElementTree.Element]] = {}
    for testcase in testcases:
        key = (testcase.get("classname", ""), testcase.get("name", ""))
        indexed.setdefault(key, []).append(testcase)

    expected_skips: set[tuple[str, str]] = set()
    mapped: set[tuple[str, str]] = set()
    for case in cases:
        case_id = case.get("id")
        status = case.get("status")
        if not isinstance(case_id, str):
            issues.append("acceptance_case_missing_id")
            continue
        references = case.get("tests" if status == "passed" else "partial_tests", [])
        if status == "passed" and not references:
            issues.append(f"{case_id}:no_tests_mapped")
        if not isinstance(references, list):
            issues.append(f"{case_id}:invalid_test_mapping")
            continue
        for reference in references:
            if not isinstance(reference, str):
                issues.append(f"{case_id}:invalid_test_reference")
                continue
            module, function = _test_key(reference)
            matches = [
                (key, testcase)
                for key, rows in indexed.items()
                if key[0] == module and (key[1] == function or key[1].startswith(function + "["))
                for testcase in rows
            ]
            if not matches:
                issues.append(f"{case_id}:mapped_test_not_collected:{reference}")
                continue
            for key, testcase in matches:
                mapped.add(key)
                if testcase.find("skipped") is not None:
                    issues.append(f"{case_id}:mapped_test_skipped:{reference}")
                if testcase.find("failure") is not None or testcase.find("error") is not None:
                    issues.append(f"{case_id}:mapped_test_failed:{reference}")
        if status == "blocked_environment":
            blocked_tests = case.get("tests", [])
            if not isinstance(blocked_tests, list):
                issues.append(f"{case_id}:invalid_blocked_test_mapping")
                continue
            for reference in blocked_tests:
                if not isinstance(reference, str):
                    issues.append(f"{case_id}:invalid_blocked_test_reference")
                    continue
                module, function = _test_key(reference)
                matches = [
                    (key, testcase)
                    for key, rows in indexed.items()
                    if key[0] == module
                    and (key[1] == function or key[1].startswith(function + "["))
                    for testcase in rows
                ]
                if not matches:
                    issues.append(f"{case_id}:blocked_test_not_collected:{reference}")
                for key, testcase in matches:
                    expected_skips.add(key)
                    if testcase.find("skipped") is None:
                        issues.append(f"{case_id}:blocked_test_not_skipped:{reference}")

    observed_skips = {
        (testcase.get("classname", ""), testcase.get("name", ""))
        for testcase in testcases
        if testcase.find("skipped") is not None
    }
    for module, name in sorted(observed_skips - expected_skips):
        issues.append(f"unexpected_skip:{module}::{name}")
    for testcase in testcases:
        if testcase.find("failure") is not None or testcase.find("error") is not None:
            issues.append(
                f"test_failed:{testcase.get('classname', '')}::{testcase.get('name', '')}"
            )
    blocked_ids = [
        str(case.get("id", "<missing>"))
        for case in cases
        if case.get("status") == "blocked_environment"
    ]
    evidence_consistent = not issues
    return {
        "evidence_consistent": evidence_consistent,
        "release_ready": (
            evidence_consistent
            and spec_path is not None
            and not blocked_ids
            and counts["passed"] == len(cases)
        ),
        "spec_coverage_checked": spec_path is not None,
        "blocked_acceptance_ids": blocked_ids,
        "collected_testcases": len(testcases),
        "mapped_executed_testcases": len(mapped),
        "expected_skips": len(expected_skips),
        "observed_skips": len(observed_skips),
        "acceptance_total": len(cases),
        "issues": sorted(set(issues)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit", type=Path)
    parser.add_argument("acceptance", type=Path)
    parser.add_argument(
        "--spec", type=Path, help="require exact acceptance IDs from the design specification"
    )
    parser.add_argument(
        "--require-release-ready",
        action="store_true",
        help="fail when any acceptance remains blocked, failed, or unrun",
    )
    args = parser.parse_args(argv)
    if args.require_release_ready and args.spec is None:
        parser.error("--require-release-ready also requires --spec")
    try:
        result = check_report(args.junit, args.acceptance, args.spec)
    except (OSError, ValueError, ElementTree.ParseError, yaml.YAMLError) as exc:
        print(
            json.dumps(
                {"evidence_consistent": False, "release_ready": False, "error": str(exc)},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    if not result["evidence_consistent"]:
        return 2
    if args.require_release_ready and not result["release_ready"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
