"""Opt-in single-principal development worker; never a production auth adapter."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from time import sleep
from uuid import uuid4

from sqlalchemy import Engine

from hnh.adapters.blobs.filesystem import FileBlobStore
from hnh.adapters.models.deepseek_responses import DeepSeekResponsesProvider
from hnh.adapters.postgres.database import build_engine
from hnh.application.action_gateway import ActionGateway
from hnh.application.auth import DevelopmentAuthenticator
from hnh.application.capabilities import CapabilityRegistry
from hnh.application.completion import CompletionGate
from hnh.application.context import ContextBuilder
from hnh.application.resources import ResourceStore
from hnh.application.run_controller import RunController
from hnh.application.run_worker import RunWorker
from hnh.application.runner import Runner
from hnh.config import Settings
from hnh.domain.errors import HarnessError, LeaseLost
from hnh.domain.identity import TrustedContext
from hnh.ports.models import ModelProvider


def build_development_worker(
    settings: Settings,
    *,
    engine: Engine | None = None,
    provider: ModelProvider | None = None,
    authenticator: DevelopmentAuthenticator | None = None,
    worker_id: str | None = None,
    cancellation_only: bool = False,
    lease_seconds: int = 30,
) -> RunWorker:
    """Assemble one trusted development principal, optionally without a model."""
    missing = [
        name
        for name, value in (
            ("HNH_DATABASE_URL", settings.database_url),
            ("HNH_DEV_TOKEN", settings.development_token),
        )
        if not value
    ]
    if provider is None and not cancellation_only:
        missing.extend(
            name
            for name, value in (("HNH_DEEPSEEK_API_KEY", settings.deepseek_api_key),)
            if not value
        )
    if missing:
        raise ValueError(f"development worker configuration missing: {', '.join(missing)}")
    assert settings.database_url is not None
    assert settings.development_token is not None
    resolved_engine = engine if engine is not None else build_engine(settings.database_url)
    resolved_auth = authenticator if authenticator is not None else settings.authenticator()

    def resolve_actor(tenant_id: str, subject_id: str) -> TrustedContext | None:
        try:
            current = resolved_auth.authenticate(
                f"Bearer {settings.development_token}", "runs:read"
            )
        except HarnessError:
            return None
        if (current.tenant_id, current.subject_id) != (tenant_id, subject_id):
            return None
        return current

    controller = RunController(resolved_engine)
    runner: Runner | None = None
    if not cancellation_only:
        resolved_provider = provider
        if resolved_provider is None:
            assert settings.deepseek_api_key is not None
            resolved_provider = DeepSeekResponsesProvider(
                api_key=settings.deepseek_api_key,
                model=settings.deepseek_model,
                base_url=settings.deepseek_base_url,
                reasoning_effort=settings.deepseek_reasoning_effort,
            )
        registry = CapabilityRegistry()
        blob_store = FileBlobStore(Path(settings.blob_root)) if settings.blob_root else None
        resources = ResourceStore(resolved_engine, blob_store)
        runner = Runner(
            controller,
            ContextBuilder(controller, registry),
            ActionGateway(resolved_engine, registry, resources, controller),
            CompletionGate(resolved_engine, controller),
            resolved_provider,
        )
    return RunWorker(
        controller,
        runner,
        resolve_actor,
        worker_id=worker_id or f"dev-{uuid4().hex}",
        cancellation_only=cancellation_only,
        lease_seconds=lease_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Opt-in local bounded Harness worker")
    parser.add_argument("--once", action="store_true", help="Claim at most one due Run job")
    parser.add_argument(
        "--cancel-only",
        action="store_true",
        help="Process only cancellation jobs without a model provider",
    )
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--lease-seconds", type=int, default=30)
    args = parser.parse_args(argv)
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    if args.lease_seconds < 3:
        parser.error("--lease-seconds must be at least 3")
    try:
        worker = build_development_worker(
            Settings.from_environment(),
            cancellation_only=args.cancel_only,
            lease_seconds=args.lease_seconds,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        while True:
            try:
                step = worker.run_once()
            except LeaseLost:
                # A successor owns the lease; this process must not commit it.
                step = None
            if args.once:
                return 0
            if step is None:
                sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
