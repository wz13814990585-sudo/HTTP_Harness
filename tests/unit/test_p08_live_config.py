from __future__ import annotations

from pathlib import Path

import pytest

from hnh.evaluation_live import LiveEchoConfig, live_echo_cases


def test_live_config_fails_closed_without_external_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "HNH_DATABASE_URL",
        "HNH_DEEPSEEK_API_KEY",
        "HNH_DEEPSEEK_MODEL",
        "HNH_DEEPSEEK_BASE_URL",
        "HNH_DEEPSEEK_REASONING_EFFORT",
        "HNH_MCP_ENDPOINT",
        "HNH_MCP_ISSUER",
        "HNH_MCP_RESOURCE",
        "HNH_MCP_CLIENT_ID",
        "HNH_MCP_CLIENT_SECRET",
        "HNH_EVAL_OUTPUT",
        "HNH_EVAL_TENANT",
        "HNH_EVAL_SUBJECT",
        "HNH_EVAL_IMPLEMENTATION_REVISION",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="HNH_DEEPSEEK_API_KEY"):
        LiveEchoConfig.from_environment()


def test_echo_suite_has_four_distinct_predeclared_oracles() -> None:
    cases = live_echo_cases()
    assert len(cases) == 4
    assert len({case.case_id for case in cases}) == 4
    assert len({case.expected_evidence for case in cases}) == 4
    assert all(case.expected_evidence[0].removeprefix("echo:") in case.task for case in cases)


def test_live_config_requires_an_explicit_safe_implementation_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    values = {
        "HNH_DATABASE_URL": "postgresql+psycopg://localhost/test",
        "HNH_DEEPSEEK_API_KEY": "test-only-not-real",
        "HNH_DEEPSEEK_MODEL": "deepseek-v4-pro",
        "HNH_DEEPSEEK_REASONING_EFFORT": "max",
        "HNH_MCP_ENDPOINT": "https://mcp.example.test/tools",
        "HNH_MCP_ISSUER": "https://issuer.example.test",
        "HNH_MCP_RESOURCE": "https://mcp.example.test",
        "HNH_MCP_CLIENT_ID": "client-test",
        "HNH_MCP_CLIENT_SECRET": "secret-test",
        "HNH_EVAL_OUTPUT": str(tmp_path / "raw.jsonl"),
        "HNH_EVAL_TENANT": "tenant-test",
        "HNH_EVAL_SUBJECT": "subject-test",
        "HNH_EVAL_IMPLEMENTATION_REVISION": "git:0123456789abcdef",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    config = LiveEchoConfig.from_environment()
    assert config.implementation_revision == "git:0123456789abcdef"
    assert config.model == "deepseek-v4-pro"
    assert config.base_url == "https://api.deepseek.com"
    assert config.reasoning_effort == "max"

    monkeypatch.setenv("HNH_DEEPSEEK_MODEL", "deepseek-chat")
    with pytest.raises(ValueError, match="unsupported DeepSeek model"):
        LiveEchoConfig.from_environment()
    monkeypatch.setenv("HNH_DEEPSEEK_MODEL", "deepseek-v4-pro")

    monkeypatch.setenv("HNH_EVAL_IMPLEMENTATION_REVISION", "unversioned working tree")
    with pytest.raises(ValueError, match="IMPLEMENTATION_REVISION"):
        LiveEchoConfig.from_environment()
