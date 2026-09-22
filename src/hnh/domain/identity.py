from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TrustedContext:
    """Identity established by trusted transport authentication."""

    tenant_id: str
    subject_id: str
    scopes: frozenset[str]
