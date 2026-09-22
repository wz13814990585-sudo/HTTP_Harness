#!/usr/bin/env python3
"""Static consistency checks for a DESIGN package, not a Harness test suite.

Dependencies: PyYAML, jsonschema. Does not start an API, database, model,
MCP client or sandbox; never treats static checks as production evidence.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import yaml
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError as exc:
    raise SystemExit(
        "Install PyYAML and jsonschema in your development environment "
        "before running this static checker."
    ) from exc

ROOT = Path(__file__).resolve().parents[1]
checks: list[dict[str, Any]] = []


def check(name: str, run: Callable[[], Any]) -> None:
    try:
        detail = run()
    except Exception as exc:
        checks.append({"name": name, "status": "failed", "detail": f"{type(exc).__name__}: {exc}"})
    else:
        checks.append(
            {
                "name": name,
                "status": "passed",
                "detail": str(detail if detail is not None else "OK"),
            }
        )


def load_json(path: str | Path) -> Any:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def load_yaml(path: str | Path) -> Any:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def require(value: Any, message: str) -> None:
    if not value:
        raise AssertionError(message)


def walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def pointer(document: Any, ref: str) -> Any:
    require(ref.startswith("#/"), f"Expected local JSON pointer, found: {ref}")
    out = document
    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        out = out[int(key)] if isinstance(out, list) else out[key]
    return out


def validate_refs(doc: Any) -> int:
    count = 0
    for node in walk(doc):
        if "$ref" in node:
            pointer(doc, node["$ref"])
            count += 1
    return count


# Do not validate reports from a previous run as input specifications.
inputs = [
    p
    for p in ROOT.rglob("*")
    if p.is_file()
    and p.suffix in {".json", ".yaml", ".yml"}
    and "reports" not in p.relative_to(ROOT).parts
]
for p in sorted(inputs):
    rel = p.relative_to(ROOT)
    check(
        f"parse:{rel}",
        lambda p=p: load_json(p) is not None if p.suffix == ".json" else load_yaml(p) is not None,
    )

schema = load_json("contracts/schemas/contracts.schema.json")
for p in sorted((ROOT / "contracts/schemas").glob("*.json")):

    def validate_schema_file(p=p):
        s = load_json(p)
        Draft202012Validator.check_schema(s)
        return f"JSON Schema 2020-12 meta-schema valid; {validate_refs(s)} local references resolve"

    check(f"schema:{p.name}", validate_schema_file)

for name, definition in schema["$defs"].items():
    check(
        f"definition:{name}",
        lambda definition=definition: Draft202012Validator.check_schema(definition),
    )

manifest = load_json("contracts/examples/manifest.json")
for item in manifest:

    def validate_example(item=item):
        composed = {
            "$schema": schema["$schema"],
            "$defs": schema["$defs"],
            "$ref": f"#/$defs/{item['schema']}",
        }
        validator = Draft202012Validator(composed, format_checker=FormatChecker())
        errors = list(validator.iter_errors(load_json(item["path"])))
        if item["valid"]:
            require(not errors, "; ".join(e.message for e in errors))
            return "Positive example accepted"
        require(bool(errors), "Deliberately invalid example was not rejected")
        return "Negative example correctly rejected: " + errors[0].message

    check(f"example:{item['path']}", validate_example)

api = load_yaml("contracts/openapi.yaml")
check("openapi:local_refs", lambda: f"{validate_refs(api)} local references resolve")


def api_structure():
    require(api["openapi"] == "3.1.1", "Compatibility target must be explicitly 3.1.1")
    require(api.get("security"), "Global authentication required")
    ids: set[str] = set()
    total = 0
    for path, item in api["paths"].items():
        require(path.startswith("/v1/"), f"Unexpected path {path}")
        names = set(re.findall(r"\{([^{}]+)\}", path))
        for method, op in item.items():
            if method not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            total += 1
            require(
                op.get("operationId") not in ids and op.get("operationId"),
                f"Unique operationId required: {path}",
            )
            ids.add(op["operationId"])
            params = item.get("parameters", []) + op.get("parameters", [])
            resolved = [pointer(api, p["$ref"]) if "$ref" in p else p for p in params]
            actual = {p["name"] for p in resolved if p["in"] == "path"}
            require(names == actual, f"Path parameter mismatch: {method} {path}")
            require(
                all(p.get("required") for p in resolved if p["in"] == "path"),
                "Path parameters must be required",
            )
            if method in {"post", "put"}:
                require(
                    any(
                        p.get("in") == "header"
                        and p.get("name", "").lower() == "idempotency-key"
                        and p.get("required")
                        for p in resolved
                    ),
                    f"Missing idempotency admission contract: {path}",
                )
            require(
                op.get("security", api["security"]),
                f"Anonymous access unexpectedly enabled: {path}",
            )
            require("default" in op["responses"], f"No error response: {path}")
            if path.endswith("/responses") or path.endswith("/reconciliations"):
                require(
                    op.get("x-hnh-agent-access") is False,
                    f"Model has administrative authority: {path}",
                )
                require(
                    any(p.get("name") == "If-Match" and p.get("required") for p in resolved),
                    f"Administrative CAS missing: {path}",
                )
    return (
        f"{len(api['paths'])} paths; {total} operations; "
        "route/auth/idempotency/CAS checks passed. Not a complete OpenAPI validator."
    )


check("openapi:basic_contract_structure", api_structure)

machines = load_json("contracts/state_machines.json")


def state_consistency():
    for entity, type_name in [("run", "Run"), ("action", "Action")]:
        machine = machines[entity]
        known = set(machine["transitions"])
        require(
            known == set(schema["$defs"][type_name]["properties"]["status"]["enum"]),
            f"States disagree for {entity}",
        )
        require(machine["initial"] in known, "Missing initial state")
        for old, targets in machine["transitions"].items():
            require(set(targets) <= known, f"Unknown transition target {entity}:{old}")
            require(len(targets) == len(set(targets)), "Duplicate state transition")
            if old in machine["terminal"]:
                require(not targets, f"Terminal state can revive: {entity}:{old}")
        seen = {machine["initial"]}
        while True:
            expanded = seen | {t for s in seen for t in machine["transitions"][s]}
            if expanded == seen:
                break
            seen = expanded
        require(seen == known, f"Unreachable states in {entity}: {known - seen}")
    require("unknown_to_ready" in machines["guards"], "Unknown-effect reissue guard missing")
    return (
        "Run/Action enums, reachability, terminal immutability and "
        "declared unknown-effect guard match"
    )


check("state_machines:consistency", state_consistency)

board = load_json("prompts/taskboard.json")
acceptance = load_yaml("eval/acceptance_cases.yaml")


def task_graph():
    all_ids = {s["id"] for s in board["stages"]}
    done = set()
    for stage in board["stages"]:
        require(set(stage["depends_on"]) <= done, f"Order/cycle/dependency error at {stage['id']}")
        require((ROOT / stage["prompt"]).is_file(), f"Missing prompt {stage['prompt']}")
        require(
            stage["status"] == "not_started", "Design bundle must not assert runtime completion"
        )
        done.add(stage["id"])
    require(len(all_ids) == len(board["stages"]), "Duplicate stage IDs")
    all_cases = {c["id"]: c for c in acceptance["cases"]}
    require(len(all_cases) == len(acceptance["cases"]), "Duplicate AT IDs")
    for c in all_cases.values():
        require(c["stage"] in all_ids, "Acceptance case references unknown stage")
        for field in ["setup", "trigger", "expected", "evidence", "tier"]:
            require(c.get(field), f"Missing acceptance specification {c['id']}:{field}")
        require(
            c["status"] == "specified_not_implemented",
            "Specifications must not be marked as executed",
        )
    assigned = set()
    for stage in board["stages"]:
        require(
            set(stage["acceptance_ids"])
            == {i for i, c in all_cases.items() if c["stage"] == stage["id"]},
            f"AT coverage mismatch for {stage['id']}",
        )
        assigned.update(stage["acceptance_ids"])
    require(assigned == set(all_cases), "Unassigned acceptance specifications")
    return (
        f"{len(all_ids)} ordered stages and {len(all_cases)} unexecuted "
        "acceptance specifications have valid cross-references"
    )


check("roadmap:acceptance_coverage", task_graph)


def config_guards():
    config = load_yaml("config/harness.example.yaml")
    # Assert absence of unsafe boolean flags generically, without assuming config internals.
    for node in walk(config):
        for key, value in node.items():
            if key in {
                "allow_host_exec",
                "allow_pickle_import",
                "anonymous_access",
                "allow_anonymous",
                "advertise_untested",
                "advertise_untested_extensions",
            }:
                require(value is False, f"Unsafe default {key}={value}")
    require(config["sandbox"]["required_for_code"] is True, "Code isolation required")
    require(config["sandbox"]["network"] == "deny", "Default sandbox network must be denied")
    require(config["context"]["allow_secrets"] is False, "Secrets may not enter context")
    require(
        config["model"]["hosted_side_effect_tools"] is False,
        "Hosted effects must not bypass gateway",
    )
    return "Configured safety switches checked; runtime enforcement not tested"


check("config:safety_declarations", config_guards)


def required_documents():
    needed = [
        "README.md",
        "AGENTS.md",
        "PLANS.md",
        "CODEX_MASTER_PROMPT.md",
        "docs/SOURCES.md",
        "docs/00_research.md",
        "docs/01_architecture.md",
        "docs/02_http_contract.md",
        "docs/03_runtime_recovery.md",
        "docs/04_security.md",
        "docs/05_model_context_strategy.md",
        "docs/06_mcp_adapters.md",
        "docs/07_data_storage.md",
        "docs/08_testing_evaluation.md",
        "docs/09_operations.md",
        "docs/10_roadmap.md",
        "docs/11_adrs.md",
        "prompts/REVIEW_PROMPT.md",
    ]
    for rel in needed:
        require((ROOT / rel).is_file() and (ROOT / rel).stat().st_size > 0, f"Missing/empty {rel}")
    # Markdown links to local existing files; ignore external URLs and fragments.
    count = 0
    for p in ROOT.rglob("*.md"):
        if "reports" in p.relative_to(ROOT).parts:
            continue
        for link in re.findall(r"\[[^\]]+\]\(([^)]+)\)", p.read_text(encoding="utf-8")):
            if "://" in link or link.startswith("#"):
                continue
            local = link.split("#", 1)[0]
            require((p.parent / local).exists(), f"Broken markdown link in {p.name}: {link}")
            count += 1
    return f"{len(needed)} required documents; {count} explicit local markdown links resolve"


check("documents:completeness", required_documents)

failed = [c for c in checks if c["status"] == "failed"]
report = {
    "report_type": "design_package_static_validation",
    "created_at_utc": datetime.now(UTC).isoformat(),
    "scope": (
        "JSON/YAML parsing, JSON Schema meta-validation, positive/negative examples, "
        "local refs, basic OpenAPI shape, state machines, planned stages and document "
        "consistency only."
    ),
    "not_performed": [
        "Harness implementation tests",
        "Database/concurrency/recovery tests",
        "Sandbox/security penetration tests",
        "Live model/provider calls",
        "MCP SDK/server interoperability",
        "Full OpenAPI standard conformance validator",
        "Performance benchmarks",
    ],
    "summary": {
        "checks": len(checks),
        "passed": len(checks) - len(failed),
        "failed": len(failed),
        "schema_definitions": len(schema["$defs"]),
        "example_cases": len(manifest),
        "openapi_paths": len(api["paths"]),
        "stages": len(board["stages"]),
        "unexecuted_acceptance_specifications": len(acceptance["cases"]),
    },
    "checks": checks,
}
(ROOT / "reports").mkdir(exist_ok=True)
(ROOT / "reports/design_validation.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
lines = [
    "# Design package static validation",
    "",
    f"Generated (UTC): {report['created_at_utc']}",
    "",
    "**This is not a Harness runtime test or production readiness report.**",
    "",
    f"Checks: {len(checks)}; passed: {len(checks) - len(failed)}; failed: {len(failed)}.",
    "",
    (
        f"Definitions: {len(schema['$defs'])}; examples: {len(manifest)}; "
        f"OpenAPI paths: {len(api['paths'])}; stages: {len(board['stages'])}; "
        f"unexecuted acceptance specs: {len(acceptance['cases'])}."
    ),
    "",
    "Full results: `design_validation.json`.",
    "",
    "## Not performed",
    "",
]
lines += ["- " + s for s in report["not_performed"]]
if failed:
    lines += ["", "## Failures", ""] + ["- " + c["name"] + ": " + c["detail"] for c in failed]
(ROOT / "reports/design_validation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
for c in failed:
    print("FAIL", c["name"], c["detail"], file=sys.stderr)
raise SystemExit(1 if failed else 0)
