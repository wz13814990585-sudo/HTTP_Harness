from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx2
from mcp.client.auth import TokenStorage
from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from hnh.application.credentials import OAuthClientCredentialProvider
from hnh.application.mcp_catalog import MCPIntegrationBinding
from hnh.domain.errors import InvalidOperation
from hnh.domain.identity import TrustedContext


class InMemoryOAuthTokenStorage(TokenStorage):
    """Non-durable local-test storage; never a production secret store."""

    def __init__(self) -> None:
        self._tokens: OAuthToken | None = None
        self._client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self._tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self._client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._client_info = client_info


class IssuerPinnedOAuthMCPTransport:
    """Official SDK client-credentials auth, bound to trusted issuer/resource/actor."""

    def __init__(
        self,
        binding: MCPIntegrationBinding,
        context: TrustedContext,
        vault: OAuthClientCredentialProvider,
        storage: TokenStorage,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        if (
            binding.credential_reference is None
            or binding.issuer is None
            or binding.resource is None
        ):
            raise ValueError("OAuth MCP binding needs credential reference, issuer and resource")
        self.binding = binding
        self.context = context
        self.vault = vault
        self.storage = storage
        self.transport = transport

    @asynccontextmanager
    async def __call__(self, url: str) -> AsyncIterator[Any]:
        async with self.build_client(url) as client:
            async with streamable_http_client(url, http_client=client) as streams:
                yield streams

    def build_client(self, url: str) -> httpx2.AsyncClient:
        """Construct the exact auth client also used by the MCP stream transport."""
        resource = urlsplit(self.binding.resource or "")
        endpoint = urlsplit(url)
        issuer = urlsplit(self.binding.issuer or "")
        resource_path = resource.path.rstrip("/")
        if (
            resource.scheme != "https"
            or issuer.scheme != "https"
            or endpoint.scheme != "https"
            or not resource.hostname
            or not issuer.hostname
            or not endpoint.hostname
            or resource.username is not None
            or resource.password is not None
            or resource.fragment
            or issuer.username is not None
            or issuer.password is not None
            or issuer.fragment
            or endpoint.netloc != resource.netloc
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.fragment
            or (
                resource_path
                and endpoint.path != resource_path
                and not endpoint.path.startswith(resource_path + "/")
            )
        ):
            raise InvalidOperation("OAuth MCP endpoint or issuer binding is invalid")
        assert self.binding.credential_reference is not None
        assert self.binding.issuer is not None
        assert self.binding.resource is not None
        credentials = self.vault.resolve_client(
            self.binding.credential_reference,
            integration_id=self.binding.integration_id,
            tenant_id=self.context.tenant_id,
            issuer=self.binding.issuer,
            resource=self.binding.resource,
            subject_id=self.context.subject_id,
        )
        auth = ClientCredentialsOAuthProvider(
            server_url=url,
            storage=self.storage,
            client_id=credentials.client_id,
            client_secret=credentials.client_secret,
            issuer=self.binding.issuer,
        )
        return httpx2.AsyncClient(
            auth=auth,
            transport=self.transport,
            follow_redirects=False,
            trust_env=False,
        )
