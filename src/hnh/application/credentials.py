from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from hnh.domain.errors import PermissionDenied, ResourceNotFound


@dataclass(frozen=True, slots=True)
class CredentialBinding:
    reference: str
    integration_id: str
    issuer: str
    resource: str
    subject_id: str
    access_token: str = field(repr=False)
    tenant_id: str = field(kw_only=True)
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.expires_at is not None and self.expires_at.utcoffset() is None:
            raise ValueError("credential expiry must be timezone-aware")


class CredentialProvider(Protocol):
    """Trusted source of an already-issued, audience-bound upstream token."""

    def resolve(
        self,
        reference: str,
        *,
        integration_id: str,
        tenant_id: str,
        issuer: str,
        resource: str,
        subject_id: str,
    ) -> str: ...


class InMemoryCredentialVault:
    """Test/development vault with production-style exact binding checks."""

    def __init__(self, bindings: tuple[CredentialBinding, ...] = ()) -> None:
        self._bindings = {item.reference: item for item in bindings}
        self._revoked: set[str] = set()

    def revoke(self, reference: str) -> None:
        if reference not in self._bindings:
            raise ResourceNotFound("credential")
        self._revoked.add(reference)

    def resolve(
        self,
        reference: str,
        *,
        integration_id: str,
        tenant_id: str,
        issuer: str,
        resource: str,
        subject_id: str,
    ) -> str:
        binding = self._bindings.get(reference)
        if binding is None:
            raise ResourceNotFound("credential")
        if reference in self._revoked or (
            binding.expires_at is not None and binding.expires_at <= datetime.now(UTC)
        ):
            raise PermissionDenied()
        if (
            binding.integration_id != integration_id
            or binding.tenant_id != tenant_id
            or binding.issuer != issuer
            or binding.resource != resource
            or binding.subject_id != subject_id
        ):
            raise PermissionDenied()
        return binding.access_token


@dataclass(frozen=True, slots=True)
class OAuthClientCredentialBinding:
    reference: str
    integration_id: str
    tenant_id: str
    issuer: str
    resource: str
    subject_id: str
    client_id: str
    client_secret: str = field(repr=False)
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.expires_at is not None and self.expires_at.utcoffset() is None:
            raise ValueError("OAuth client credential expiry must be timezone-aware")


class OAuthClientCredentialProvider(Protocol):
    def resolve_client(
        self,
        reference: str,
        *,
        integration_id: str,
        tenant_id: str,
        issuer: str,
        resource: str,
        subject_id: str,
    ) -> OAuthClientCredentialBinding: ...


class InMemoryOAuthClientVault:
    """Local-test client credential vault; production must use managed secrets."""

    def __init__(self, bindings: tuple[OAuthClientCredentialBinding, ...] = ()) -> None:
        self._bindings = {item.reference: item for item in bindings}
        self._revoked: set[str] = set()

    def revoke(self, reference: str) -> None:
        if reference not in self._bindings:
            raise ResourceNotFound("credential")
        self._revoked.add(reference)

    def resolve_client(
        self,
        reference: str,
        *,
        integration_id: str,
        tenant_id: str,
        issuer: str,
        resource: str,
        subject_id: str,
    ) -> OAuthClientCredentialBinding:
        binding = self._bindings.get(reference)
        if binding is None:
            raise ResourceNotFound("credential")
        if reference in self._revoked or (
            binding.expires_at is not None and binding.expires_at <= datetime.now(UTC)
        ):
            raise PermissionDenied()
        if (
            binding.integration_id != integration_id
            or binding.tenant_id != tenant_id
            or binding.issuer != issuer
            or binding.resource != resource
            or binding.subject_id != subject_id
        ):
            raise PermissionDenied()
        return binding
