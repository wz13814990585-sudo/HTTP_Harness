from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from hnh.domain.errors import InvalidOperation

Resolver = Callable[[str], Iterable[str]]


@dataclass(frozen=True, slots=True)
class ValidatedDestination:
    url: str
    origin: tuple[str, str, int]
    addresses: tuple[str, ...]


class EgressPolicy:
    """Validate every outbound hop; connection code must use the returned addresses."""

    def __init__(
        self,
        allowed_hosts: frozenset[str],
        *,
        resolver: Resolver | None = None,
        allowed_schemes: frozenset[str] = frozenset({"https"}),
        allowed_ports: frozenset[int] = frozenset({443}),
        max_redirects: int = 3,
    ) -> None:
        self.allowed_hosts = frozenset(host.lower().rstrip(".") for host in allowed_hosts)
        self.resolver = resolver or self._system_resolver
        self.allowed_schemes = allowed_schemes
        self.allowed_ports = allowed_ports
        self.max_redirects = max_redirects

    def validate(self, url: str) -> ValidatedDestination:
        parsed = urlsplit(url)
        if parsed.scheme not in self.allowed_schemes or parsed.username or parsed.password:
            raise InvalidOperation("outbound URL scheme or credentials are not allowed")
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host or host not in self.allowed_hosts:
            raise InvalidOperation("outbound host is not registered")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise InvalidOperation("outbound port is invalid") from exc
        if port not in self.allowed_ports:
            raise InvalidOperation("outbound port is not allowed")
        addresses = tuple(dict.fromkeys(self.resolver(host)))
        if not addresses:
            raise InvalidOperation("outbound host did not resolve")
        for address in addresses:
            self._validate_address(address)
        return ValidatedDestination(url, (parsed.scheme, host, port), addresses)

    def redirect(
        self,
        previous: ValidatedDestination,
        location: str,
        headers: dict[str, str],
        *,
        redirect_count: int,
    ) -> tuple[ValidatedDestination, dict[str, str]]:
        if redirect_count >= self.max_redirects:
            raise InvalidOperation("outbound redirect limit exceeded")
        destination = self.validate(urljoin(previous.url, location))
        forwarded = dict(headers)
        if destination.origin != previous.origin:
            for name in tuple(forwarded):
                if name.lower() in {"authorization", "cookie", "proxy-authorization"}:
                    forwarded.pop(name)
        return destination, forwarded

    @staticmethod
    def _validate_address(address: str) -> None:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise InvalidOperation("resolver returned an invalid address") from exc
        if not parsed.is_global:
            raise InvalidOperation("outbound address is not globally routable")

    @staticmethod
    def _system_resolver(host: str) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                str(item[4][0]) for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            )
        )
