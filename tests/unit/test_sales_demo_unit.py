from __future__ import annotations

from pathlib import Path

import pytest

from hnh.ports.effects import AmbiguousDispatch
from hnh.sales_demo import (
    EXPECTED_REPORT,
    SALES_CSV,
    PublicationDriver,
    PublicationSimulator,
    publication_capability,
    report_python,
)


def test_sales_fixture_and_report_contract_are_deterministic(repository_root: Path) -> None:
    assert SALES_CSV.count("\n") == 7
    assert "2026-08-01,North,Widget A,10,120.00" in SALES_CSV
    assert "- Total units: 40" in EXPECTED_REPORT
    assert "- Total revenue: $6,010.00" in EXPECTED_REPORT
    assert "| North | 15 | $2,200.00 |" in EXPECTED_REPORT
    code = report_python(SALES_CSV)
    assert 'Path("/workspace/outputs")' in code
    assert 'output / "sales-report.md"' in code
    assert "Decimal" in code
    assert (repository_root / "examples/sales_demo/sales.csv").read_text() == SALES_CSV
    assert (
        repository_root / "examples/sales_demo/expected_report.md"
    ).read_text() == EXPECTED_REPORT


def test_publication_simulator_commits_before_losing_response_and_is_queryable() -> None:
    with PublicationSimulator() as simulator:
        driver = PublicationDriver(simulator.base_url)
        try:
            with pytest.raises(AmbiguousDispatch):
                driver.dispatch(
                    "action-demo",
                    {
                        "artifact_id": "artifact-demo",
                        "artifact_sha256": "a" * 64,
                        "title": "Monthly Sales Summary",
                    },
                    None,
                )
            evidence = driver.reconcile("action-demo", None, None)
        finally:
            driver.close()

    assert evidence is not None
    assert evidence.resolution == "confirmed_applied"
    assert evidence.evidence_ids == ("remote-receipt:publication-1",)
    assert simulator.ledger.requests == 1
    assert simulator.ledger.queries == 1
    assert len(simulator.ledger.publications) == 1


def test_publication_capability_requires_exact_approval_and_is_unsafe() -> None:
    capability = publication_capability()

    assert capability.id == "demo.report.publish"
    assert capability.requires_approval is True
    assert capability.effect_semantics == "unsafe"
    assert capability.required_scope == "integrations:invoke"
