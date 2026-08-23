"""Network tool handlers — **Phase 6 replaces this file** with the real
implementations ported from ``skill/job_discovery/runtime/*``
(playwright_worker / classify_url / wechat / career_sheets) plus the
public-URL guard.

Until then every handler is a deterministic stub that fails closed: raising
``tool_execution_failed`` so a model can never believe a page was fetched,
searched, or classified.  Signatures follow the shared handler contract
``(context, input) -> output_model`` so the registry wiring does not change
when the real implementations land.
"""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..errors import TOOL_EXECUTION_FAILED, CareerToolError

#: Raised by every stub until Phase 6 lands the real implementations.
_PHASE6_MESSAGE = "network handler ported in phase 6"


def _not_ported(context: ToolContext, payload: Any) -> Any:
    """Shared stub body — fail closed, never pretend success."""
    del context, payload
    raise CareerToolError(TOOL_EXECUTION_FAILED, _PHASE6_MESSAGE)


def fetch_public_job_pages(context: ToolContext, payload: Any) -> Any:
    """Stub — batch fetch of public job pages (Phase 6)."""
    return _not_ported(context, payload)


def fetch_public_job_page(context: ToolContext, payload: Any) -> Any:
    """Stub — single public job page fetch (Phase 6)."""
    return _not_ported(context, payload)


def search_public_job_pages(context: ToolContext, payload: Any) -> Any:
    """Stub — public job-page search (Phase 6)."""
    return _not_ported(context, payload)


def query_career_sheet_records(context: ToolContext, payload: Any) -> Any:
    """Stub — career smartsheet record query (Phase 6)."""
    return _not_ported(context, payload)


def fetch_wechat_article(context: ToolContext, payload: Any) -> Any:
    """Stub — WeChat article OCR fetch (Phase 6)."""
    return _not_ported(context, payload)


def classify_job_url(context: ToolContext, payload: Any) -> Any:
    """Stub — low-budget URL classification (Phase 6)."""
    return _not_ported(context, payload)


__all__ = [
    "fetch_public_job_pages",
    "fetch_public_job_page",
    "search_public_job_pages",
    "query_career_sheet_records",
    "fetch_wechat_article",
    "classify_job_url",
]
