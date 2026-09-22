"""A localhost HTTP effect ledger with a real response-loss fault window.

This is test-only infrastructure. The server commits a publication in its own
ledger, then closes the TCP connection before sending the first HTTP response.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from hnh.ports.effects import (
    AmbiguousDispatch,
    EffectCancellation,
    EffectDispatchResult,
    EffectReconciliation,
)


@dataclass(slots=True)
class EffectLedger:
    publications: list[dict[str, Any]] = field(default_factory=list)
    requests: int = 0
    queries: int = 0
    _lock: Lock = field(default_factory=Lock)

    def publish(self, action_id: str, payload: dict[str, Any]) -> tuple[str, bool]:
        with self._lock:
            self.requests += 1
            handle = f"publication-{len(self.publications) + 1}"
            self.publications.append({"action_id": action_id, "payload": payload, "handle": handle})
            return handle, self.requests == 1

    def get(self, action_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self.queries += 1
            return [dict(item) for item in self.publications if item["action_id"] == action_id]


class ControlledEffectServer:
    def __init__(self, *, drop_first_response: bool = True) -> None:
        self.ledger = EffectLedger()
        self.drop_first_response = drop_first_response
        ledger = self.ledger

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path != "/publish":
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 65536:
                        raise ValueError
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict) or not isinstance(body.get("action_id"), str):
                        raise ValueError
                    payload = body.get("payload")
                    if not isinstance(payload, dict):
                        raise ValueError
                except (ValueError, json.JSONDecodeError):
                    self.send_error(400)
                    return
                handle, first = ledger.publish(body["action_id"], payload)
                if first and drop_first_response:
                    # The external effect has committed in this independent
                    # ledger. The client must now observe a transport error.
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                self._json(201, {"handle": handle, "applied": True})

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                if not parsed.path.startswith("/effects/"):
                    self.send_error(404)
                    return
                action_id = parsed.path.removeprefix("/effects/")
                self._json(200, {"publications": ledger.get(action_id)})

            def _json(self, status: int, value: dict[str, Any]) -> None:
                encoded = json.dumps(value, sort_keys=True).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format: str, *_args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> ControlledEffectServer:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class ControlledHTTPEffectDriver:
    """Test-only wire adapter; a real integration must use registered egress policy."""

    def __init__(self, base_url: str) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=2,
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def dispatch(
        self,
        action_id: str,
        operation: dict[str, Any],
        downstream_idempotency_key: str | None,
    ) -> EffectDispatchResult:
        headers = (
            {"Idempotency-Key": downstream_idempotency_key}
            if downstream_idempotency_key is not None
            else None
        )
        try:
            response = self._client.post(
                "/publish",
                json={"action_id": action_id, "payload": operation},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise AmbiguousDispatch({"transport_error": type(exc).__name__}) from exc
        value = response.json()
        return EffectDispatchResult(
            response.status_code,
            value,
            applied=value.get("applied") is True,
            upstream_handle=value.get("handle"),
        )

    def reconcile(
        self,
        action_id: str,
        upstream_handle: str | None,
        downstream_idempotency_key: str | None,
    ) -> EffectReconciliation | None:
        del downstream_idempotency_key
        response = self._client.get(f"/effects/{quote(action_id, safe='')}")
        response.raise_for_status()
        publications = response.json()["publications"]
        if len(publications) != 1:
            return None
        handle = publications[0]["handle"]
        if upstream_handle is not None and handle != upstream_handle:
            return None
        return EffectReconciliation(
            "confirmed_applied",
            (f"remote-receipt:{handle}",),
            "independent HTTP effect ledger confirms one publication",
        )

    def cancel(self, action_id: str, upstream_handle: str | None) -> EffectCancellation:
        del action_id, upstream_handle
        return EffectCancellation(False, False, {"reason": "test server has no cancellation"})
