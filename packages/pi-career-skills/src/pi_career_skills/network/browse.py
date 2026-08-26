"""Browser automation tool handlers: browse-public-job-page + search-job-site.

These are the P0-P2 browser tools on top of :mod:`.playwright_worker`'s shared
automation kernel:

- ``browse_public_job_page`` — open one public career URL in headless Chromium
  and interact: consent dismissal (with security hard gates), load-all
  scrolling, URL-pattern pagination, or card click-through.  The output is a
  full ``FetchPublicJobPageOutput`` evidence page (same classification /
  normalization contract as ``fetch_public_job_page``) plus the steering
  signals the agent needs to decide the next step.
- ``search_job_site`` — drive the site's own in-site search box and return the
  post-search page as evidence, with search diagnostics (was the box found,
  did the card count actually change, what does the result indicator say).

Both stay inside the same security envelope as every other network tool:
public URLs only, no login, no anti-bot bypass (consent clicks are hard-gated
against captcha / login-wall / WeChat-verification markers), and the worker
routes all sub-resources through the public-URL guard.  The governor's polite
rate limit is respected, but its page cache is deliberately NOT used — a
cached requests-path page would be stale for an interactive render.
"""

from __future__ import annotations

from typing import Any

from ..business.job_discovery.models import (
    BrowsePublicJobPageInput,
    BrowsePublicJobPageOutput,
    SearchJobSiteInput,
    SearchJobSiteOutput,
)
from . import playwright_worker
from .page_fetch import (
    _build_evidence_page,
    _ensure_domain_available,
    _remember_blocked_domain,
)
from .request_governor import before_request as _govern_before_request
from .url_guard import PublicFetchError, _assert_public_url

_BROWSE_SIGNAL_KEYS = (
    "cards_visible",
    "estimated_total_items",
    "pagination_pattern",
    "strategy",
    "strategy_detail",
    "pages_collected",
    "warning",
)


def _signals_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in _BROWSE_SIGNAL_KEYS}


def _evidence_from_payload(
    requested_url: str, payload: dict[str, Any]
):
    """Run the one classification/normalization contract on a browse payload."""
    return _build_evidence_page(
        requested_url=requested_url,
        effective_url=payload.get("effective_url") or requested_url,
        title=payload.get("title"),
        visible_text=payload.get("body") or "",
        status_code=payload.get("status_code"),
        detail_links=payload.get("links") or [],
    )


def browse_public_job_page(
    context: Any, payload: BrowsePublicJobPageInput
) -> BrowsePublicJobPageOutput:
    """Open a public career URL in a headless browser and interact with it.

    ``mode`` selects the automation depth (render / load-all / paginate /
    interact; see the input model).  The result is a full evidence page —
    identical classification contract to ``fetch-public-job-page`` — plus the
    steering signals (``cards_visible``, ``estimated_total_items``,
    ``pagination_pattern``, ``strategy``, ``warning``) so the agent can decide
    what to do next on this site.  Use it when ``fetch-public-job-page``
    returns a ``js_shell`` or ``list_only`` page that needs in-browser
    interaction to become a full JD.
    """
    _assert_public_url(payload.url)
    _ensure_domain_available(context, payload.url)
    _govern_before_request(context, payload.url)
    try:
        data = playwright_worker._browse_with_playwright(
            payload.url,
            mode=payload.mode,
            pages=payload.pages,
            max_cards=payload.max_cards,
            wait_ms=payload.wait_ms,
        )
    except PublicFetchError as error:
        _remember_blocked_domain(context, payload.url, error)
        raise
    page = _evidence_from_payload(payload.url, data)
    return BrowsePublicJobPageOutput(
        **page.model_dump(),
        **_signals_from_payload(data),
    )


def search_job_site(
    context: Any, payload: SearchJobSiteInput
) -> SearchJobSiteOutput:
    """Search a career site's own in-site search box for a job keyword.

    Returns the post-search page as evidence (so the results are immediately
    extractable) plus search diagnostics: whether a search box was found,
    how the visible card count changed, any result-count indicator text, and
    a warning when the post-search count equals the pre-search count (a
    likely client-side fake filter that left the full list in the DOM).

    Distinct from ``search-public-job-pages`` (external web search): this is
    the site's native index.
    """
    _assert_public_url(payload.url)
    _ensure_domain_available(context, payload.url)
    _govern_before_request(context, payload.url)
    try:
        data = playwright_worker._browse_with_playwright(
            payload.url,
            mode="search",
            max_cards=payload.max_cards,
            wait_ms=1_500,
            term=payload.query,
        )
    except PublicFetchError as error:
        _remember_blocked_domain(context, payload.url, error)
        raise
    page = _evidence_from_payload(payload.url, data)
    return SearchJobSiteOutput(
        url=payload.url,
        query=payload.query,
        effective_url=data.get("effective_url") or page.effective_url,
        search_ok=bool(data.get("search_ok")),
        search_detail=data.get("search_detail"),
        pre_search_card_count=data.get("pre_search_card_count"),
        post_search_card_count=data.get("post_search_card_count"),
        result_indicator=data.get("result_indicator"),
        warning=data.get("warning"),
        artifact_id=page.artifact_id,
        source_url=page.source_url,
        content_hash=page.content_hash,
        visible_text=page.visible_text,
        title=page.title,
        http_status=page.http_status,
        quality=page.quality,
        quality_signal=page.quality_signal,
        detail_links=page.detail_links,
    )


__all__ = [
    "browse_public_job_page",
    "search_job_site",
    "_signals_from_payload",
    "_evidence_from_payload",
]
