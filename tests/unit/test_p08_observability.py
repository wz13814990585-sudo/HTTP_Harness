from __future__ import annotations

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from hnh.application.auth import DevelopmentAuthenticator, DevelopmentPrincipal
from hnh.config import Settings
from hnh.transport.http.app import create_app
from hnh.transport.http.observability import build_otlp_provider


def test_at_067_missing_dependencies_are_explicit_and_no_mock_readiness() -> None:
    app = create_app(settings=Settings(database_url=None, development_token=None))
    client = TestClient(app)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["core_ready"] is False
    assert response.json()["components"] == {
        "database": "unavailable",
        "model_configuration": "missing",
        "isolated_broker_configuration": "missing",
        "blob_store_configuration": "missing",
        "mcp_integrations": "disabled",
    }
    assert "not proof" in response.json()["note"]


def test_metrics_are_scope_protected_low_cardinality_and_traceparent_is_safe() -> None:
    auth = DevelopmentAuthenticator(
        {"ops-token": DevelopmentPrincipal("tenant", "operator", frozenset({"metrics:read"}))}
    )
    client = TestClient(
        create_app(settings=Settings(database_url=None, development_token=None), authenticator=auth)
    )
    inbound = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    response = client.get("/readyz", headers={"traceparent": inbound})
    assert response.headers["traceparent"].startswith("00-0123456789abcdef0123456789abcdef-")
    assert response.headers["traceparent"] != inbound
    assert client.get("/metrics").status_code == 401
    metrics = client.get("/metrics", headers={"Authorization": "Bearer ops-token"})
    assert metrics.status_code == 200
    assert 'route="/readyz"' in metrics.text
    assert 'status_class="5xx"' in metrics.text
    assert "ops-token" not in metrics.text
    assert "0123456789abcdef0123456789abcdef" not in metrics.text


def test_at_067_http_span_exports_safe_route_and_matches_response_context() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    auth = DevelopmentAuthenticator(
        {"secret-token": DevelopmentPrincipal("tenant", "operator", frozenset({"metrics:read"}))}
    )
    app = create_app(
        settings=Settings(database_url=None, development_token=None),
        authenticator=auth,
        tracer_provider=provider,
    )
    parent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    with TestClient(app) as client:
        response = client.get("/readyz?secret=do-not-export", headers={"traceparent": parent})
        assert response.status_code == 503
        response_context = response.headers["traceparent"].split("-")
        assert response_context[1] == parent.split("-")[1]
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "GET /readyz"
        assert span.context is not None
        assert response_context[2] == f"{span.context.span_id:016x}"
        assert span.parent is not None
        assert span.parent.span_id == int(parent.split("-")[2], 16)
        assert span.attributes is not None
        assert span.attributes["http.route"] == "/readyz"
        assert span.attributes["http.response.status_code"] == 503
        assert "secret-token" not in str(span.attributes)
        assert "do-not-export" not in str(span.attributes)
    provider.shutdown()


def test_otlp_endpoint_rejects_cleartext_remote_and_embedded_credentials() -> None:
    for endpoint in (
        "http://example.com/v1/traces",
        "https://user:pass@example.com/v1/traces",
        "https://example.com/v1/traces?token=secret",
        "https://example.com/not-traces",
    ):
        try:
            build_otlp_provider(endpoint)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe OTLP endpoint was accepted: {endpoint}")
