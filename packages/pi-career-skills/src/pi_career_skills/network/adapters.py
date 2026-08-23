"""A1 certified-adapter channel: fail-closed and OFF unless assembly opts in.

Verbatim ports from ``skill/job_discovery/runtime/job_discovery.py``:

- the adapter gate (300-344): ``_PUBLIC_API_ADAPTERS_ENABLED`` /
  ``_ADAPTERS_PACKAGE`` / ``_ADAPTERS_SCRIPTS_DIR`` / ``_adapter_package`` /
  ``enable_public_api_adapters``;
- ``_adapter_company_for_url`` (1639-1671), ``_host_to_company`` (1674-1684),
  ``_run_company_adapter`` (1687-1698), ``_fetch_via_adapter`` (1701-1724).

The adapters package lives under ``skill/job-discovery/scripts`` in the
source tree; in this package that directory does not exist, so
``_adapter_package()`` returns None and every adapter route degrades to the
normal requests/Playwright chain (fail closed, never a fabricated record).
A covered URL that fails while the channel is enabled is a hard
``adapter:<code>`` blocked error.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..business.job_discovery.models import FetchPublicJobPageOutput
from .url_guard import PublicFetchError

#: Adapter channel is OFF unless runtime assembly opts in.
_PUBLIC_API_ADAPTERS_ENABLED = False
_ADAPTERS_PACKAGE: Any | None = None
_ADAPTERS_SCRIPTS_DIR = str(
    Path(__file__).resolve().parents[3] / "skill" / "job-discovery" / "scripts"
)


def enable_public_api_adapters(enabled: bool) -> None:
    """Toggle the A1 certified-adapter channel (called from runtime assembly)."""
    global _PUBLIC_API_ADAPTERS_ENABLED
    _PUBLIC_API_ADAPTERS_ENABLED = enabled


def _adapter_package() -> Any | None:
    """Load the skill adapters package once; None when it cannot be imported.

    The sys.path injection is idempotent and only adds the skill scripts
    directory (same shape as the deepagents ``browse_fetch`` loader).
    """
    global _ADAPTERS_PACKAGE
    if _ADAPTERS_PACKAGE is None:
        scripts_dir = _ADAPTERS_SCRIPTS_DIR
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        try:
            import adapters  # noqa: PLC0415 - lazy, guarded by callers
        except Exception:  # noqa: BLE001 - untrusted skill boundary.
            return None
        _ADAPTERS_PACKAGE = adapters
    return _ADAPTERS_PACKAGE


def _adapter_company_for_url(url: str) -> str | None:
    """Adapter company covering ``url``, or None (uncovered / unavailable).

    Packages exposing the certified ``_ADAPTERS`` registry (company ->
    class with class-level ``hosts``) are matched from that registry with
    no adapter instantiation -- each adapter ``__init__`` builds an httpx
    client (~1s on Windows), so instantiating per lookup is ~5s.  The
    registry is authoritative: a miss means the URL is uncovered.  A
    package without the registry shape falls back to its own
    ``company_for_url`` so the untrusted-boundary semantics stay intact.
    """
    package = _adapter_package()
    if package is None:
        return None
    registry = getattr(package, "_ADAPTERS", None)
    if isinstance(registry, dict):
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            host = ""
        if host:
            try:
                matched = _host_to_company(registry, host)
            except Exception:  # noqa: BLE001 - untrusted adapter boundary.
                matched = None
            if matched is not None:
                return matched
        return None
    try:
        company = package.company_for_url(url)
    except Exception:  # noqa: BLE001 - untrusted adapter boundary.
        return None
    return company if isinstance(company, str) and company else None


def _host_to_company(registry: dict[str, Any], host: str) -> str | None:
    """Host -> company from certified adapter classes, no instantiation.

    Mirrors the adapters package's own matching (exact host or suffix
    under a ``*.`` wildcard pattern); None when no class claims the host.
    """
    for company, adapter_cls in registry.items():
        for pattern in getattr(adapter_cls, "hosts", ()):
            if host == pattern or host.endswith("." + pattern.lstrip("*.")):
                return company if isinstance(company, str) and company else None
    return None


def _run_company_adapter(package: Any, url: str, company: str) -> list[dict[str, Any]]:
    """Execute one certified adapter; any failure is an ``adapter:<code>`` block."""
    try:
        adapter = package.load_company_adapter(company)
        result = adapter.execute(url, None, None)
    except Exception as exc:  # noqa: BLE001 - untrusted adapter boundary.
        code = getattr(exc, "code", "unexpected")
        raise PublicFetchError(f"adapter:{code}") from exc
    records = result.get("records") if isinstance(result, dict) else None
    if not isinstance(records, list) or not records:
        raise PublicFetchError("adapter:empty_result")
    return records


def _fetch_via_adapter(url: str) -> FetchPublicJobPageOutput | None:
    """Adapter-first fetch for a certified company URL; None when uncovered.

    Adapter evidence is the same memory-bound shape as browsed evidence: a
    JSON document of normalized records whose sha256 is the content hash, so
    ``_with_observed_page`` and the extract side treat it like any page.
    """
    if not _PUBLIC_API_ADAPTERS_ENABLED:
        return None
    company = _adapter_company_for_url(url)
    if company is None:
        return None
    package = _adapter_package()
    records = _run_company_adapter(package, url, company)
    body = json.dumps(records, ensure_ascii=False, indent=2)
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    first_title = records[0].get("title") if isinstance(records[0], dict) else None
    return FetchPublicJobPageOutput(
        artifact_id=f"observed:{content_hash}",
        source_url=url,
        title=first_title if isinstance(first_title, str) else None,
        visible_text=body,
        content_hash=content_hash,
    )


__all__ = [
    "_PUBLIC_API_ADAPTERS_ENABLED",
    "_ADAPTERS_PACKAGE",
    "_ADAPTERS_SCRIPTS_DIR",
    "enable_public_api_adapters",
    "_adapter_package",
    "_adapter_company_for_url",
    "_host_to_company",
    "_run_company_adapter",
    "_fetch_via_adapter",
]
