"""A native function-tool view over the trusted capability catalog.

This is only a model-facing codec. It never executes a tool or grants authority;
the decoded HttpOperation still passes through ActionGateway.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from jsonschema import Draft202012Validator

from hnh.application.operations import HttpOperation
from hnh.domain.errors import ModelOutputInvalid


class FunctionToolCatalog:
    def __init__(self, capabilities: list[dict[str, Any]]) -> None:
        self._capabilities: dict[str, dict[str, Any]] = {}
        self.tools: list[dict[str, Any]] = []
        for capability in capabilities:
            identifier = str(capability["id"])
            slug = re.sub(r"[^a-zA-Z0-9_-]", "_", identifier)[:42]
            digest = hashlib.sha256(identifier.encode()).hexdigest()[:12]
            name = f"cap_{slug}_{digest}"
            if name in self._capabilities:
                raise ValueError("capability function name collision")
            schema = capability["input_schema"]
            Draft202012Validator.check_schema(schema)
            self._capabilities[name] = capability
            self.tools.append(
                {
                    "type": "function",
                    "name": name,
                    "description": capability["description"],
                    "parameters": schema,
                    # Capability schemas include optional/oneOf fields. The
                    # gateway still validates the complete binding below.
                    "strict": False,
                }
            )

    def decode(self, name: str, arguments: str) -> HttpOperation:
        capability = self._capabilities.get(name)
        if capability is None:
            raise ModelOutputInvalid("unknown or unauthorized function name")
        try:
            parameters = json.loads(arguments)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ModelOutputInvalid("function arguments are not valid JSON") from exc
        if not isinstance(parameters, dict):
            raise ModelOutputInvalid("function arguments must be an object")
        errors = sorted(
            Draft202012Validator(capability["input_schema"]).iter_errors(parameters),
            key=lambda item: (list(map(str, item.absolute_path)), item.message),
        )
        if errors:
            raise ModelOutputInvalid(
                f"function arguments do not match capability: {errors[0].message}"
            )

        target = str(capability["path_template"])
        remaining = dict(parameters)
        for field in re.findall(r"\{([^{}]+)\}", target):
            value = remaining.pop(field, None)
            if not isinstance(value, str) or not value:
                raise ModelOutputInvalid(f"function path parameter {field} is missing")
            if field != "file_path" and "/" in value:
                raise ModelOutputInvalid(f"function path parameter {field} contains a separator")
            target = target.replace("{" + field + "}", value)

        method = str(capability["method"])
        media_types = capability["request_media_types"]
        headers: dict[str, str] = {}
        payload: dict[str, Any] | None = None
        if method in {"GET", "HEAD"}:
            if remaining:
                raise ModelOutputInvalid("read function has unexpected body parameters")
        elif "text/plain" in media_types and ("code" in remaining or "text" in remaining):
            field = "code" if "code" in remaining else "text"
            value = remaining.pop(field)
            if not isinstance(value, str):
                raise ModelOutputInvalid("text function body must be a string")
            headers["content-type"] = "text/plain"
            payload = {"kind": "text", "text": value}
        else:
            headers["content-type"] = str(media_types[0]) if media_types else "application/json"
            payload = {"kind": "json", "value": remaining}
            remaining = {}

        if "if_match" in remaining:
            headers["if-match"] = str(remaining.pop("if_match"))
        if "if_none_match" in remaining:
            headers["if-none-match"] = str(remaining.pop("if_none_match"))
        if remaining:
            raise ModelOutputInvalid(
                "function parameters cannot be represented as a bound operation"
            )
        try:
            return HttpOperation.model_validate(
                {"method": method, "target": target, "headers": headers, "payload": payload}
            )
        except ValueError as exc:
            raise ModelOutputInvalid("function produced an invalid HTTP operation") from exc
