"""DNS-resolving public-URL guard for every outbound network tool.

Port of ``skill/job_discovery/runtime/job_discovery.py`` ``_is_public_url``
(1114-1137) / ``_assert_public_url`` (1140-1155) and the fetch error carrier
``PublicJobFetchError`` (456-479).  Every outbound request in this package
passes ``_assert_public_url`` before connecting; redirect hops are
re-validated on every ``Location``; and DNS must resolve to globally
routable addresses only (loopback / RFC1918 / link-local / multicast /
cloud-metadata are all rejected).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from ..errors import UNSAFE_PUBLIC_URL, CareerToolError

#: errors.py has no PUBLIC_HOST_UNRESOLVABLE constant (it is off-limits for
#: modification); the literal string is the source skill's exact error code.
PUBLIC_HOST_UNRESOLVABLE = "public_host_unresolvable"


class PublicFetchError(CareerToolError):
    """Stable, non-sensitive public-web fetch failure (source PublicJobFetchError).

    ``code`` is the stable machine-testable identity the adapter maps into a
    ToolObservation (blocked codes -> status "blocked", everything else ->
    "failed").  The optional ``effective_url`` / ``redirect_chain`` /
    ``status_code`` keep fetch provenance available to failure records.
    """

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        effective_url: str | None = None,
        redirect_chain: list[str] | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(code, message or "")
        self.effective_url = effective_url
        self.redirect_chain = list(redirect_chain or [])
        self.status_code = status_code

    def __str__(self) -> str:
        if self.message:
            return self.message
        return self.code


def _is_public_url(url: str) -> bool:
    """True only for http(s), userinfo-free hosts resolving to a global IP.

    Returns False for non-http(s) schemes, embedded credentials, unresolvable
    hosts, and non-global (loopback / RFC1918 / link-local / cloud-metadata)
    addresses -- a permissive check used by the Playwright route guard, which
    must fail closed on any ambiguous destination.
    """
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return False
    try:
        addresses = socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return all(
        ipaddress.ip_address(sockaddr[0]).is_global
        for _family, _kind, _proto, _canon, sockaddr in addresses
    )


def _assert_public_url(url: str) -> None:
    """Raise ``unsafe_public_url`` / ``public_host_unresolvable`` when *url*
    is not a globally routable public http(s) URL (source verbatim)."""
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise PublicFetchError(UNSAFE_PUBLIC_URL)
    try:
        addresses = socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise PublicFetchError(PUBLIC_HOST_UNRESOLVABLE) from exc
    for _family, _kind, _proto, _canon, sockaddr in addresses:
        if not ipaddress.ip_address(sockaddr[0]).is_global:
            raise PublicFetchError(UNSAFE_PUBLIC_URL)


__all__ = [
    "PUBLIC_HOST_UNRESOLVABLE",
    "PublicFetchError",
    "_assert_public_url",
    "_is_public_url",
]
