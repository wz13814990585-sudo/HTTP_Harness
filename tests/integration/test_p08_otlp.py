"""A local Collector-shaped receiver proves that OTLP trace bytes are emitted."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from threading import Thread

from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from hnh.config import Settings
from hnh.transport.http.app import create_app


def test_at_067_configured_otlp_exporter_sends_redacted_route_span() -> None:
    payloads: Queue[tuple[str, str, bytes]] = Queue()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            payloads.put((self.path, self.headers.get("Content-Type", ""), body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as receiver:
        thread = Thread(target=receiver.serve_forever, daemon=True)
        thread.start()
        try:
            app = create_app(
                settings=Settings(
                    database_url=None,
                    development_token=None,
                    otlp_traces_endpoint=f"http://127.0.0.1:{receiver.server_port}/v1/traces",
                )
            )
            with TestClient(app) as client:
                response = client.get("/readyz?secret=do-not-export")
                assert response.status_code == 503
                assert app.state.tracer_provider.force_flush(timeout_millis=5000)
            path, content_type, body = payloads.get(timeout=5)
            assert path == "/v1/traces"
            assert content_type == "application/x-protobuf"
            request = ExportTraceServiceRequest()
            request.ParseFromString(body)
            spans = [
                span
                for resource in request.resource_spans
                for scope in resource.scope_spans
                for span in scope.spans
            ]
            assert len(spans) == 1
            span = spans[0]
            assert span.name == "GET /readyz"
            assert span.trace_id.hex() == response.headers["traceparent"].split("-")[1]
            attributes = {attribute.key: attribute.value for attribute in span.attributes}
            assert attributes["http.route"].string_value == "/readyz"
            assert attributes["http.response.status_code"].int_value == 503
            assert b"do-not-export" not in body
        finally:
            receiver.shutdown()
            thread.join(timeout=5)


def test_at_067_otlp_exporter_does_not_follow_redirect_to_other_host() -> None:
    first_posts: Queue[bool] = Queue()
    redirected_posts: Queue[bool] = Queue()

    class Destination(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            redirected_posts.put(True)
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Destination) as destination:

        class Redirector(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                first_posts.put(True)
                self.send_response(307)
                self.send_header(
                    "Location", f"http://127.0.0.1:{destination.server_port}/v1/traces"
                )
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Redirector) as redirector:
            threads = [
                Thread(target=server.serve_forever, daemon=True)
                for server in (destination, redirector)
            ]
            for thread in threads:
                thread.start()
            try:
                app = create_app(
                    settings=Settings(
                        database_url=None,
                        development_token=None,
                        otlp_traces_endpoint=(
                            f"http://127.0.0.1:{redirector.server_port}/v1/traces"
                        ),
                    )
                )
                with TestClient(app) as client:
                    assert client.get("/healthz").status_code == 200
                    assert app.state.tracer_provider.force_flush(timeout_millis=5000)
                assert first_posts.get(timeout=5) is True
                assert redirected_posts.empty()
            finally:
                for server in (redirector, destination):
                    server.shutdown()
                for thread in threads:
                    thread.join(timeout=5)
