from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _cases() -> list[Any]:
    root = Path(__file__).resolve().parents[2]
    manifest = load_json(root / "contracts" / "examples" / "manifest.json")
    return [pytest.param(item, id=Path(item["path"]).stem) for item in manifest]


@pytest.mark.parametrize("item", _cases())
def test_contract_example_matches_manifest(repository_root: Path, item: dict[str, Any]) -> None:
    contract = load_json(repository_root / "contracts" / "schemas" / "contracts.schema.json")
    composed = {
        "$schema": contract["$schema"],
        "$defs": contract["$defs"],
        "$ref": f"#/$defs/{item['schema']}",
    }
    validator = Draft202012Validator(composed, format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(load_json(repository_root / item["path"])),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )

    if item["valid"]:
        assert not errors, [
            {"path": list(error.absolute_path), "message": error.message} for error in errors
        ]
    else:
        assert errors, f"deliberately invalid example was accepted: {item['path']}"
        assert errors[0].absolute_path is not None
