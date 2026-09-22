from __future__ import annotations

import re
import secrets
from collections import defaultdict
from ipaddress import ip_address
from threading import Lock
from urllib.parse import urlsplit

import requests
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanContext

TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def response_traceparent(incoming: str | None) -> str:
    match = TRACEPARENT.fullmatch(incoming or "")
    trace_id = match.group(1) if match and match.group(1) != "0" * 32 else secrets.token_hex(16)
    flags = match.group(3) if match else "01"
    return f"00-{trace_id}-{secrets.token_hex(8)}-{flags}"


def span_traceparent(context: SpanContext) -> str:
    return f"00-{context.trace_id:032x}-{context.span_id:016x}-{int(context.trace_flags):02x}"


def build_otlp_provider(endpoint: str) -> TracerProvider:
    """Admin-configured exporter; never accept a destination from model operations."""
    parsed = urlsplit(endpoint)
    host = parsed.hostname
    try:
        loopback = host == "localhost" or (host is not None and ip_address(host).is_loopback)
    except ValueError:
        loopback = False
    if (
        parsed.scheme not in {"http", "https"}
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/v1/traces")
        or (parsed.scheme == "http" and not loopback)
    ):
        raise ValueError("HNH_OTLP_TRACES_ENDPOINT must be HTTPS or loopback HTTP /v1/traces")
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: "hnh-api"}))
    session = requests.Session()
    session.max_redirects = 0
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, session=session))
    )
    return provider


class HTTPMetrics:
    """Process-local low-cardinality request metrics; never a durable event source."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counts: dict[tuple[str, str, str], int] = defaultdict(int)
        self._sums: dict[tuple[str, str, str], float] = defaultdict(float)
        self._buckets: dict[tuple[str, str, str, float], int] = defaultdict(int)

    def observe(self, method: str, route: str, status_code: int, seconds: float) -> None:
        key = (method, route, f"{status_code // 100}xx")
        with self._lock:
            self._counts[key] += 1
            self._sums[key] += seconds
            for bucket in BUCKETS:
                if seconds <= bucket:
                    self._buckets[(*key, bucket)] += 1

    def render(self) -> str:
        lines = [
            "# TYPE hnh_http_requests_total counter",
            "# TYPE hnh_http_request_duration_seconds histogram",
        ]
        with self._lock:
            for key in sorted(self._counts):
                method, route, status_class = key
                labels = f'method="{method}",route="{route}",status_class="{status_class}"'
                lines.append(f"hnh_http_requests_total{{{labels}}} {self._counts[key]}")
                for bucket in BUCKETS:
                    lines.append(
                        "hnh_http_request_duration_seconds_bucket"
                        f'{{{labels},le="{bucket}"}} {self._buckets.get((*key, bucket), 0)}'
                    )
                lines.append(
                    "hnh_http_request_duration_seconds_bucket"
                    f'{{{labels},le="+Inf"}} {self._counts[key]}'
                )
                lines.append(
                    f"hnh_http_request_duration_seconds_sum{{{labels}}} {self._sums[key]:.9f}"
                )
                lines.append(
                    f"hnh_http_request_duration_seconds_count{{{labels}}} {self._counts[key]}"
                )
        return "\n".join(lines) + "\n"
