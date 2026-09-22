"""Load and compare the target API contract with the implemented ASGI surface."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put"})


def repository_root() -> Path:
    """Find the source checkout containing the design contracts.

    Installed deployments must provide packaged contracts before they can
    advertise the target API. P00 intentionally supports only a source checkout.
    """

    for parent in Path(__file__).resolve().parents:
        if (parent / "contracts" / "openapi.yaml").is_file():
            return parent
    raise RuntimeError("contracts/openapi.yaml was not found in this source checkout")


def load_target_openapi() -> dict[str, Any]:
    document = yaml.safe_load(
        (repository_root() / "contracts" / "openapi.yaml").read_text(encoding="utf-8")
    )
    if not isinstance(document, dict):
        raise TypeError("contracts/openapi.yaml must contain an object")
    return document


def iter_operations(document: Mapping[str, Any]) -> Iterator[tuple[str, str, Mapping[str, Any]]]:
    paths = document.get("paths")
    if not isinstance(paths, Mapping):
        raise TypeError("OpenAPI paths must be an object")
    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, Mapping):
            raise TypeError("OpenAPI path entries must be objects keyed by strings")
        for method, operation in path_item.items():
            if method.lower() in HTTP_METHODS:
                if not isinstance(operation, Mapping):
                    raise TypeError(f"OpenAPI operation {method} {path} must be an object")
                yield path, method.lower(), operation


@dataclass(frozen=True, slots=True)
class ContractSurfaceDiff:
    """A deterministic, serializable comparison of target and runtime surfaces."""

    implemented: tuple[str, ...]
    unimplemented: tuple[str, ...]
    unexpected: tuple[str, ...]
    operation_id_mismatches: tuple[str, ...]


def _operation_map(document: Mapping[str, Any]) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for path, method, operation in iter_operations(document):
        operation_id = operation.get("operationId")
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError(f"Missing operationId for {method.upper()} {path}")
        result[(path, method)] = operation_id
    return result


def compare_surfaces(target: Mapping[str, Any], runtime: Mapping[str, Any]) -> ContractSurfaceDiff:
    target_operations = _operation_map(target)
    runtime_operations = _operation_map(runtime)
    target_keys = set(target_operations)
    runtime_keys = set(runtime_operations)
    shared = target_keys & runtime_keys

    def label(key: tuple[str, str]) -> str:
        path, method = key
        return f"{method.upper()} {path}"

    mismatches = tuple(
        sorted(
            f"{label(key)}: target={target_operations[key]} runtime={runtime_operations[key]}"
            for key in shared
            if target_operations[key] != runtime_operations[key]
        )
    )
    return ContractSurfaceDiff(
        implemented=tuple(sorted(label(key) for key in shared)),
        unimplemented=tuple(sorted(label(key) for key in target_keys - runtime_keys)),
        unexpected=tuple(sorted(label(key) for key in runtime_keys - target_keys)),
        operation_id_mismatches=mismatches,
    )


def main() -> None:
    """Print the current target/runtime contract difference as JSON."""

    from hnh.transport.http.app import create_app

    diff = compare_surfaces(load_target_openapi(), create_app().openapi())
    print(json.dumps(asdict(diff), ensure_ascii=False, indent=2))
