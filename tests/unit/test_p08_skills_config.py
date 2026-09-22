from __future__ import annotations

from pathlib import Path

import pytest

from hnh.evaluation_skills import LiveSkillsConfig


def test_live_skills_config_requires_credentials_and_fresh_raw_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    names = (
        "HNH_DATABASE_URL",
        "HNH_OPENAI_API_KEY",
        "HNH_OPENAI_MODEL",
        "HNH_EVAL_SKILLS_OUTPUT",
        "HNH_EVAL_TENANT",
        "HNH_EVAL_SUBJECT",
        "HNH_EVAL_IMPLEMENTATION_REVISION",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="HNH_OPENAI_API_KEY"):
        LiveSkillsConfig.from_environment()
    values = (
        "postgresql+psycopg://localhost/test",
        "test-only-not-real",
        "controlled-model",
        str(tmp_path / "skills.raw.jsonl"),
        "tenant-test",
        "subject-test",
        "image@sha256:0123456789abcdef",
    )
    for name, value in zip(names, values, strict=True):
        monkeypatch.setenv(name, value)
    config = LiveSkillsConfig.from_environment()
    assert config.output == tmp_path / "skills.raw.jsonl"
    assert config.implementation_revision == "image@sha256:0123456789abcdef"
    config.output.touch()
    with pytest.raises(FileExistsError):
        LiveSkillsConfig.from_environment()
