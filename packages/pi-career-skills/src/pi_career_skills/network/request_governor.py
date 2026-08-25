"""Polite, domain-scoped request governance for public recruiting pages.

This module is deliberately a *load-shedding* layer, not an anti-bot bypass.
It prevents duplicate requests, serializes requests to the same domain, and
opens a cooldown circuit after a site reports a challenge.  State is kept in
the process so separate evaluation runs in one worker can reuse successful
pages and remember a blocked site.  Callers can disable the layer in isolated
unit tests by omitting ``enforce_public_request_governor`` from metadata.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..business.job_discovery.models import FetchPublicJobPageOutput
from .url_guard import PublicFetchError

_DEFAULT_INTERVAL_SECONDS = 2.5
_DEFAULT_BLOCK_COOLDOWN_SECONDS = 30 * 60
_MAX_CACHE_ENTRIES = 256


@dataclass(frozen=True)
class _CachedPage:
    expires_at: float
    payload: dict[str, Any]


_LOCK = threading.RLock()
_LAST_REQUEST_AT: dict[str, float] = {}
_BLOCKED_UNTIL: dict[str, float] = {}
_PAGE_CACHE: dict[str, _CachedPage] = {}


def canonical_request_url(url: str) -> str:
    """Return a stable identity for request deduplication.

    Fragments are never sent to HTTP servers.  Query parameters are sorted so
    equivalent URLs share one cache key while their original display URL is
    retained in the evidence object.
    """
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    netloc = host
    if parsed.port is not None and parsed.port not in {80, 443}:
        netloc = f"{host}:{parsed.port}"
    path = parsed.path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))


def enabled(context: Any) -> bool:
    return bool(getattr(context, "metadata", {}).get("enforce_public_request_governor"))


def domain_scope(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host == "liepin.com" or host.endswith(".liepin.com"):
        return "liepin.com"
    return host


def before_request(context: Any, url: str) -> None:
    """Wait for the domain's polite interval and record the request start."""
    if not enabled(context):
        return
    domain = domain_scope(url)
    now = time.monotonic()
    interval = getattr(context, "metadata", {}).get("public_request_interval_seconds")
    if not isinstance(interval, (int, float)) or isinstance(interval, bool):
        interval = _DEFAULT_INTERVAL_SECONDS
    interval = max(0.0, min(float(interval), 30.0))
    while True:
        with _LOCK:
            previous = _LAST_REQUEST_AT.get(domain)
            wait_for = interval - (now - previous) if previous is not None else 0.0
            if wait_for <= 0:
                _LAST_REQUEST_AT[domain] = time.monotonic()
                return
        # Do not hold the global lock while waiting: requests to unrelated
        # domains may proceed, while same-domain callers re-check the clock
        # before claiming the next slot.
        time.sleep(wait_for)
        now = time.monotonic()


def ensure_available(context: Any, url: str) -> None:
    """Fail fast during a process-level site cooldown."""
    if not enabled(context):
        return
    domain = domain_scope(url)
    with _LOCK:
        until = _BLOCKED_UNTIL.get(domain, 0.0)
    if until > time.monotonic():
        raise PublicFetchError(
            "domain_temporarily_blocked",
            message=f"{domain} 处于反爬冷却窗口，暂不重新访问。",
            effective_url=url,
        )


def remember_blocked(context: Any, url: str, code: str) -> None:
    """Open a cooldown circuit for challenge/access-denied responses."""
    if not enabled(context) or code not in {"anti_bot_challenge", "access_denied"}:
        return
    cooldown = getattr(context, "metadata", {}).get("public_block_cooldown_seconds")
    if not isinstance(cooldown, (int, float)) or isinstance(cooldown, bool):
        cooldown = _DEFAULT_BLOCK_COOLDOWN_SECONDS
    cooldown = max(60.0, min(float(cooldown), 24 * 60 * 60))
    with _LOCK:
        _BLOCKED_UNTIL[domain_scope(url)] = time.monotonic() + cooldown


def get_cached_page(context: Any, url: str) -> FetchPublicJobPageOutput | None:
    if not enabled(context):
        return None
    now = time.monotonic()
    key = canonical_request_url(url)
    with _LOCK:
        cached = _PAGE_CACHE.get(key)
        if cached is None:
            return None
        if cached.expires_at <= now:
            _PAGE_CACHE.pop(key, None)
            return None
        return FetchPublicJobPageOutput.model_validate(cached.payload)


def put_cached_page(context: Any, page: FetchPublicJobPageOutput) -> None:
    if not enabled(context):
        return
    ttl = getattr(context, "metadata", {}).get("public_page_cache_ttl_seconds")
    if not isinstance(ttl, (int, float)) or isinstance(ttl, bool):
        ttl = 6 * 60 * 60
    ttl = max(60.0, min(float(ttl), 7 * 24 * 60 * 60))
    key = canonical_request_url(page.source_url)
    with _LOCK:
        if len(_PAGE_CACHE) >= _MAX_CACHE_ENTRIES and key not in _PAGE_CACHE:
            oldest = min(_PAGE_CACHE, key=lambda item: _PAGE_CACHE[item].expires_at)
            _PAGE_CACHE.pop(oldest, None)
        _PAGE_CACHE[key] = _CachedPage(
            expires_at=time.monotonic() + ttl,
            payload=page.model_dump(mode="json"),
        )


def clear_for_tests() -> None:
    """Clear process state; intended for deterministic unit tests only."""
    with _LOCK:
        _LAST_REQUEST_AT.clear()
        _BLOCKED_UNTIL.clear()
        _PAGE_CACHE.clear()


__all__ = [
    "canonical_request_url",
    "enabled",
    "domain_scope",
    "before_request",
    "ensure_available",
    "remember_blocked",
    "get_cached_page",
    "put_cached_page",
    "clear_for_tests",
]
