from __future__ import annotations

from datetime import UTC, datetime, timedelta

import anyio
import httpx2
import pytest
from mcp.client.auth import OAuthFlowError

from hnh.adapters.mcp.oauth import InMemoryOAuthTokenStorage, IssuerPinnedOAuthMCPTransport
from hnh.application.credentials import (
    InMemoryOAuthClientVault,
    OAuthClientCredentialBinding,
)
from hnh.application.mcp_catalog import MCPIntegrationBinding
from hnh.domain.errors import InvalidOperation, PermissionDenied
from hnh.domain.identity import TrustedContext

RESOURCE = "https://resource.example.test/mcp"
ISSUER = "https://issuer.example.test"
CONTEXT = TrustedContext("tenant-oauth", "subject-oauth", frozenset({"integrations:invoke"}))
BINDING = MCPIntegrationBinding(
    integration_id="oauth-test",
    profile="modern2026-07-28",
    credential_reference="credential-one",
    issuer=ISSUER,
    resource=RESOURCE,
)


def _factory(
    handler: httpx2.MockTransport,
    *,
    credential_issuer: str = ISSUER,
    expiry: datetime | None = None,
) -> tuple[IssuerPinnedOAuthMCPTransport, InMemoryOAuthClientVault]:
    vault = InMemoryOAuthClientVault(
        (
            OAuthClientCredentialBinding(
                "credential-one",
                "oauth-test",
                "tenant-oauth",
                credential_issuer,
                RESOURCE,
                "subject-oauth",
                "client-one",
                "client-secret-one",
                expires_at=expiry,
            ),
        )
    )
    return (
        IssuerPinnedOAuthMCPTransport(
            BINDING, CONTEXT, vault, InMemoryOAuthTokenStorage(), transport=handler
        ),
        vault,
    )


def _oauth_fixture(
    *,
    advertised_issuer: str = ISSUER,
    metadata_issuer: str = ISSUER,
) -> tuple[httpx2.MockTransport, list[tuple[str, str | None]]]:
    observed: list[tuple[str, str | None]] = []

    async def handle(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        observed.append((url, request.headers.get("Authorization")))
        if url == RESOURCE:
            if request.headers.get("Authorization") == "Bearer access-one":
                return httpx2.Response(200, json={"ok": True})
            return httpx2.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer resource_metadata="https://resource.example.test/'
                        '.well-known/oauth-protected-resource/mcp"'
                    )
                },
            )
        if url == "https://resource.example.test/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(
                200,
                json={"resource": RESOURCE, "authorization_servers": [advertised_issuer]},
            )
        if url == f"{advertised_issuer}/.well-known/oauth-authorization-server":
            return httpx2.Response(
                200,
                json={
                    "issuer": metadata_issuer,
                    "authorization_endpoint": f"{advertised_issuer}/authorize",
                    "token_endpoint": f"{advertised_issuer}/token",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["client_credentials"],
                    "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                },
            )
        if url == f"{advertised_issuer}/token":
            return httpx2.Response(
                200, json={"access_token": "access-one", "token_type": "Bearer", "expires_in": 3600}
            )
        return httpx2.Response(404)

    return httpx2.MockTransport(handle), observed


def test_p06_official_oauth_client_credentials_binds_issuer_and_resource() -> None:
    transport, observed = _oauth_fixture()
    factory, _vault = _factory(transport)

    async def request() -> int:
        async with factory.build_client(RESOURCE) as client:
            return (await client.get(RESOURCE)).status_code

    assert anyio.run(request) == 200
    assert any(url == f"{ISSUER}/token" for url, _authorization in observed)
    assert all(
        authorization is None or url in {RESOURCE, f"{ISSUER}/token"}
        for url, authorization in observed
    )
    assert all(
        not (authorization or "").startswith("Basic ") or url == f"{ISSUER}/token"
        for url, authorization in observed
    )
    assert any(authorization == "Bearer access-one" for _url, authorization in observed)


@pytest.mark.parametrize(
    ("advertised_issuer", "metadata_issuer"),
    [
        ("https://evil.example.test", "https://evil.example.test"),
        (ISSUER, "https://evil.example.test"),
    ],
)
def test_p06_malicious_issuer_never_receives_client_secret_or_token_request(
    advertised_issuer: str, metadata_issuer: str
) -> None:
    transport, observed = _oauth_fixture(
        advertised_issuer=advertised_issuer, metadata_issuer=metadata_issuer
    )
    factory, _vault = _factory(transport)

    async def request() -> None:
        async with factory.build_client(RESOURCE) as client:
            await client.get(RESOURCE)

    with pytest.raises(OAuthFlowError):
        anyio.run(request)
    assert all(not url.endswith("/token") for url, _authorization in observed)
    assert all("client-secret-one" not in (authorization or "") for _, authorization in observed)


def test_p06_oauth_vault_rejects_wrong_actor_expiry_revocation_and_endpoint() -> None:
    transport, _observed = _oauth_fixture()
    expired, _vault = _factory(transport, expiry=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(PermissionDenied):
        expired.build_client(RESOURCE)
    wrong_issuer, _vault = _factory(transport, credential_issuer="https://other.example.test")
    with pytest.raises(PermissionDenied):
        wrong_issuer.build_client(RESOURCE)
    factory, vault = _factory(transport)
    with pytest.raises(InvalidOperation):
        factory.build_client("https://other.example.test/mcp")
    vault.revoke("credential-one")
    with pytest.raises(PermissionDenied):
        factory.build_client(RESOURCE)


def test_p06_revocation_after_token_issuance_prevents_cached_token_reuse() -> None:
    transport, observed = _oauth_fixture()
    factory, vault = _factory(transport)

    async def request() -> int:
        async with factory.build_client(RESOURCE) as client:
            return (await client.get(RESOURCE)).status_code

    assert anyio.run(request) == 200
    assert any(authorization == "Bearer access-one" for _, authorization in observed)
    before_revocation = len(observed)
    vault.revoke("credential-one")
    with pytest.raises(PermissionDenied):
        anyio.run(request)
    assert len(observed) == before_revocation


def test_p06_resource_401_reauthorizes_with_pinned_issuer() -> None:
    valid_access = "access-one"
    exchanges = 0
    observed: list[tuple[str, str | None]] = []

    async def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal exchanges
        url = str(request.url)
        authorization = request.headers.get("Authorization")
        observed.append((url, authorization))
        if url == RESOURCE:
            if authorization == f"Bearer {valid_access}":
                return httpx2.Response(200, json={"ok": True})
            return httpx2.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer resource_metadata="https://resource.example.test/'
                        '.well-known/oauth-protected-resource/mcp"'
                    )
                },
            )
        if url == "https://resource.example.test/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(
                200,
                json={"resource": RESOURCE, "authorization_servers": [ISSUER]},
            )
        if url == f"{ISSUER}/.well-known/oauth-authorization-server":
            return httpx2.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["client_credentials"],
                    "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                },
            )
        if url == f"{ISSUER}/token":
            exchanges += 1
            return httpx2.Response(
                200,
                json={
                    "access_token": f"access-{'one' if exchanges == 1 else 'two'}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        return httpx2.Response(404)

    factory, _vault = _factory(httpx2.MockTransport(handle))

    async def request() -> int:
        async with factory.build_client(RESOURCE) as client:
            return (await client.get(RESOURCE)).status_code

    assert anyio.run(request) == 200
    assert exchanges == 1
    valid_access = "access-two"
    assert anyio.run(request) == 200
    assert exchanges == 2
    assert any(authorization == "Bearer access-one" for _, authorization in observed)
    assert any(authorization == "Bearer access-two" for _, authorization in observed)
    assert all(
        authorization is None or url in {RESOURCE, f"{ISSUER}/token"}
        for url, authorization in observed
    )
    assert all(
        not (authorization or "").startswith("Basic ") or url == f"{ISSUER}/token"
        for url, authorization in observed
    )
