from __future__ import annotations

from pathlib import Path

import pytest

from hnh.evaluation_tasks import LiveTaskConfig, live_task_fixtures


def test_task_suite_has_distinct_input_and_output_oracles() -> None:
    fixtures = live_task_fixtures()
    assert len(fixtures) == 3
    assert len({item.case_id for item in fixtures}) == 3
    assert len({item.input_text for item in fixtures}) == 3
    assert len({item.expected_text for item in fixtures}) == 3
    assert all(item.expected_text != item.input_text for item in fixtures)


def test_live_task_config_requires_explicit_credentials_and_fresh_raw_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    names = (
        "HNH_DATABASE_URL",
        "HNH_OPENAI_API_KEY",
        "HNH_OPENAI_MODEL",
        "HNH_EVAL_TASK_OUTPUT",
        "HNH_EVAL_TENANT",
        "HNH_EVAL_SUBJECT",
        "HNH_EVAL_IMPLEMENTATION_REVISION",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="HNH_OPENAI_API_KEY"):
        LiveTaskConfig.from_environment()
    values = (
        "postgresql+psycopg://localhost/test",
        "test-only-not-real",
        "controlled-model",
        str(tmp_path / "raw.jsonl"),
        "tenant-test",
        "subject-test",
        "git:0123456789abcdef",
    )
    for name, value in zip(names, values, strict=True):
        monkeypatch.setenv(name, value)
    config = LiveTaskConfig.from_environment()
    assert config.output == tmp_path / "raw.jsonl"
    assert config.implementation_revision == "git:0123456789abcdef"
    config.output.touch()
    with pytest.raises(FileExistsError):
        LiveTaskConfig.from_environment()
