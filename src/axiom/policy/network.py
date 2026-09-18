from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse


class NetworkPolicyError(ValueError):
    """Raised when a URL violates the application-level egress policy."""


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """Small application-level URL policy; this is not a firewall."""

    access: str = "public"
    allowed_hosts: tuple[str, ...] = field(default_factory=tuple)
    deny_private_addresses: bool = True

    def validate_url(self, url: str) -> None:
        if self.access == "disabled":
            raise NetworkPolicyError("network access is disabled by policy")
        if self.access not in {"public", "allowlist"}:
            raise NetworkPolicyError(f"unsupported network access mode: {self.access}")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise NetworkPolicyError("only http/https URLs are allowed")
        if parsed.username or parsed.password:
            raise NetworkPolicyError("URL credentials are not allowed")
        host = (parsed.hostname or "").rstrip(".").casefold()
        if not host:
            raise NetworkPolicyError("URL must include a hostname")
        if self.access == "allowlist" and not any(
            host == allowed or host.endswith(f".{allowed}")
            for allowed in self._normalized_hosts()
        ):
            raise NetworkPolicyError(f"host is not allowed by network policy: {host}")
        if not self.deny_private_addresses:
            return
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None:
            self._reject_private(address)
            return
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise NetworkPolicyError(f"cannot resolve host: {host}") from exc
        for info in infos:
            self._reject_private(ipaddress.ip_address(info[4][0]))

    def validate_arguments(self, arguments: dict[str, object]) -> None:
        for _key, value in _url_values(arguments):
            self.validate_url(value)

    def _normalized_hosts(self) -> tuple[str, ...]:
        return tuple(
            host.strip().rstrip(".").casefold()
            for host in self.allowed_hosts
            if host.strip()
        )

    @staticmethod
    def _reject_private(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise NetworkPolicyError("URL resolves to a private or local address")


def _url_values(value: object, key: str = ""):
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _url_values(child, str(child_key).casefold())
    elif isinstance(value, list):
        for child in value:
            yield from _url_values(child, key)
    elif key in {"url", "uri"} and isinstance(value, str) and "://" in value:
        yield key, value
