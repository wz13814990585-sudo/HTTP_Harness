from __future__ import annotations

import secrets
from dataclasses import dataclass

from hnh.domain.errors import AuthenticationRequired, PermissionDenied
from hnh.domain.identity import TrustedContext


@dataclass(frozen=True, slots=True)
class DevelopmentPrincipal:
    tenant_id: str
    subject_id: str
    scopes: frozenset[str]


class DevelopmentAuthenticator:
    """In-memory development-token verifier.

    Token values are configuration, never domain data and never returned to the
    caller. A production OIDC implementation belongs behind the same boundary.
    """

    def __init__(self, principals: dict[str, DevelopmentPrincipal]) -> None:
        self._principals = dict(principals)

    def set_scopes(self, token: str, scopes: frozenset[str]) -> None:
        principal = self._principals[token]
        self._principals[token] = DevelopmentPrincipal(
            principal.tenant_id, principal.subject_id, scopes
        )

    def authenticate(self, authorization: str | None, required_scope: str) -> TrustedContext:
        if authorization is None:
            raise AuthenticationRequired()
        scheme, separator, supplied = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not supplied:
            raise AuthenticationRequired()

        principal: DevelopmentPrincipal | None = None
        for token, candidate in self._principals.items():
            if secrets.compare_digest(supplied, token):
                principal = candidate
                break
        if principal is None:
            raise AuthenticationRequired()
        if required_scope not in principal.scopes:
            raise PermissionDenied()
        return TrustedContext(
            tenant_id=principal.tenant_id,
            subject_id=principal.subject_id,
            scopes=principal.scopes,
        )
