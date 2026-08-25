"""Bounded batch fetch of public job pages with list-page expansion.

Verbatim ports from ``skill/job_discovery/runtime/job_discovery.py``:

- IGUOPIN / Tencent public detail-route derivation (597-710);
- ``_persist_fetch_failure`` (815-834);
- ``_expand_from_list_links`` (2108-2200) and
  ``_expand_official_campus_detail`` (2203-2264);
- ``_fetch_one_with_expansion`` (2267-2415) -- WeChat / adapter single-page
  routes, requests fast path with card-list expansion, render fallback;
- ``fetch_public_job_pages`` (2418-2520) -- bounded concurrency via
  ``run_parallel_with_progress``, per-URL failure isolation, step-scoped
  ``fetched_job_urls`` dedup.

Dropped by design (documented in the task report): ``_probe_iguopin_detail_access``
(713-812, no call sites) and the TypeError shim (2441-2446, compat for a
two-arg helper contract that does not exist in this package).
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from ..business.common.batch_progress import run_parallel_with_progress
from ..business.job_discovery.models import (
    _JD_MARKER_SCAN_HEAD_CHARS,
    _JD_SECTION_MARKERS,
    FetchPublicJobPageOutput,
    FetchPublicJobPagesInput,
    FetchPublicJobPagesOutput,
    PublicJobPageFetchFailure,
    _url_shape_quality_override,
)
from . import playwright_worker
from .adapters import _fetch_via_adapter
from .page_fetch import (
    _PUBLIC_FETCH_HEADERS,
    _build_evidence_page,
    _ensure_domain_available,
    _fetch_public_page_requests,
    _fetch_public_page_requests_with_html,
    _remember_blocked_domain,
)
from .page_links import _HtmlLinkCollector, _prioritize_detail_links
from .request_governor import (
    before_request as _govern_before_request,
)
from .request_governor import (
    get_cached_page as _govern_get_cached_page,
)
from .request_governor import (
    put_cached_page as _govern_put_cached_page,
)
from .url_guard import PublicFetchError, _assert_public_url
from .wechat import _fetch_wechat_article_page

# P2 (2026-08-09): a rendered page is a JS card-list when it exposes >= this
# many same-host job-shaped detail links while carrying no JD-section text;
# the batch fetch then deep-fetches up to this many detail pages so
# match-observed-jobs sees real JD body instead of an empty card shell.
_MIN_LIST_LINKS = 2
_MAX_LIST_EXPANSION = 5
_CAMPUS_PORTAL_HOST = "career.hebut.edu.cn"
_MAX_CAMPUS_INDEX_PAGES = 2
_MAX_CAMPUS_DETAIL_PAGES = 20

_IGUOPIN_LIST_HOSTS = frozenset({"iguopin.com", "www.iguopin.com"})
_IGUOPIN_API_ORIGIN = "https://gp-api.iguopin.com"
_IGUOPIN_RECOMMEND_PATH = "/api/jobs/v1/recom-job"
_TENCENT_CAREERS_HOST = "careers.tencent.com"
_TENCENT_QUERY_PATH = "/tencentcareer/api/post/query"


def _iguopin_list_detail_urls(url: str) -> list[str]:
    """Derive public detail-page routes from IGUOPIN's anonymous list IDs.

    Only the opaque record ID is consumed. Titles, dates, requirements, and
    every other API field are deliberately ignored: the resulting public web
    pages must still be fetched and hashed before they can become evidence.
    """
    parsed = urlsplit(url)
    if (
        (parsed.hostname or "").lower() not in _IGUOPIN_LIST_HOSTS
        or not parsed.path.lower().startswith("/job")
    ):
        return []
    keyword = parse_qs(parsed.query).get("keyword", [""])[0].strip()
    if not keyword:
        return []
    recommend_url = _IGUOPIN_API_ORIGIN + _IGUOPIN_RECOMMEND_PATH
    _assert_public_url(recommend_url)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://www.iguopin.com",
        "Referer": "https://www.iguopin.com/",
        "User-Agent": _PUBLIC_FETCH_HEADERS["User-Agent"],
    }
    try:
        response = requests.post(
            recommend_url,
            json={
                "search": {"page": 1, "page_size": 20, "keyword": keyword},
                "recom": {
                    "update_time": True,
                    "company_nature": True,
                    "hot_job": True,
                },
            },
            timeout=20,
            allow_redirects=False,
            headers=headers,
        )
    except requests.RequestException:
        return []
    if response.status_code in {401, 403}:
        raise PublicFetchError(
            "iguopin_detail_api_denied", status_code=response.status_code
        )
    try:
        envelope = response.json()
    except ValueError:
        return []
    if isinstance(envelope, dict) and str(envelope.get("code")) in {"401", "403"}:
        raise PublicFetchError(
            "iguopin_detail_api_denied", status_code=int(envelope["code"])
        )
    records = envelope.get("data") if isinstance(envelope, dict) else None
    if isinstance(records, dict):
        records = records.get("list") or records.get("records") or records.get("rows")
    if not isinstance(records, list):
        return []
    urls: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        job_id = record.get("job_id") or record.get("id")
        if job_id is None:
            continue
        detail_url = "https://www.iguopin.com/job/detail?" + urlencode(
            {"id": str(job_id)}
        )
        _assert_public_url(detail_url)
        if detail_url not in urls:
            urls.append(detail_url)
    return urls


def _tencent_query_detail_urls(url: str, response_body: str) -> list[str]:
    """Derive same-origin Tencent detail routes from public query record IDs."""
    parsed = urlsplit(url)
    if (
        (parsed.hostname or "").lower() != _TENCENT_CAREERS_HOST
        or parsed.path.lower() != _TENCENT_QUERY_PATH
    ):
        return []
    try:
        envelope = json.loads(response_body)
    except (TypeError, json.JSONDecodeError):
        return []
    data = envelope.get("Data") if isinstance(envelope, dict) else None
    records = data.get("Posts") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return []
    urls: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        post_id = str(record.get("PostId") or "")
        if not re.fullmatch(r"\d{6,32}", post_id):
            continue
        detail_url = (
            "https://careers.tencent.com/jobdesc.html?"
            + urlencode({"postId": post_id})
        )
        _assert_public_url(detail_url)
        if detail_url not in urls:
            urls.append(detail_url)
    return urls


def _persist_fetch_failure(
    failure_sink: list[PublicJobPageFetchFailure],
    failure_lock: threading.Lock | None,
    *,
    source_url: str,
    error: PublicFetchError,
) -> None:
    failure = PublicJobPageFetchFailure(
        source_url=source_url,
        error_code=error.code,
        effective_url=error.effective_url,
        redirect_chain=error.redirect_chain,
        http_status=error.status_code,
        message=str(error),
    )
    if failure_lock is None:
        failure_sink.append(failure)
    else:
        with failure_lock:
            failure_sink.append(failure)


def _expand_from_list_links(
    url: str,
    links: list[str],
    list_body: str,
    *,
    context: Any | None = None,
    failure_sink: list[PublicJobPageFetchFailure] | None = None,
    failure_lock: threading.Lock | None = None,
    max_links: int | None = None,
) -> list[FetchPublicJobPageOutput]:
    """Deep-fetch detail pages behind a JS card-list, one evidence page each.

    A page is treated as a card-list only when it exposes enough same-host
    job-shaped links while carrying no JD-section text of its own (the
    campus-portal SPA family).  The JD-marker scan is head-positioned (A3):
    only the first ``_JD_MARKER_SCAN_HEAD_CHARS`` characters of the body are
    checked, because detail pages place their markers near the top while
    card shells carry them only in footer SEO text.  Detail fetches reuse
    the requests fast path with the render fallback, never recurse into
    expansion again, and fail silently per-link: the list page itself stays
    valid evidence. When a failure sink is provided, detail failures are
    returned to the caller instead of being silently discarded.
    """
    if len(links) < _MIN_LIST_LINKS:
        return []
    # A URL that is unambiguously a collection/search page is never a single
    # JD, so its job-shaped links must be expanded even when the page head
    # carries SEO/footer JD text (commercial aggregator list pages do this);
    # otherwise the model stays stuck on list-only shells and never reaches
    # the detail pages behind them.
    if _url_shape_quality_override(url) != "list_only" and any(
        marker.lower() in list_body[:_JD_MARKER_SCAN_HEAD_CHARS].lower()
        for marker in _JD_SECTION_MARKERS
    ):
        return []
    pages: list[FetchPublicJobPageOutput] = []

    def record_failure(link: str, error: PublicFetchError) -> None:
        if failure_sink is None:
            return
        _persist_fetch_failure(
            failure_sink,
            failure_lock,
            source_url=link,
            error=error,
        )

    expansion_limit = max_links if max_links is not None else _MAX_LIST_EXPANSION
    for link in _prioritize_detail_links(links)[:expansion_limit]:
        if context is not None:
            try:
                _ensure_domain_available(context, link)
            except PublicFetchError as exc:
                record_failure(link, exc)
                break
        try:
            pages.append(_fetch_public_page_requests(link))
            continue
        except PublicFetchError as exc:
            if context is not None:
                _remember_blocked_domain(context, link, exc)
            if (
                exc.code not in playwright_worker._PLAYWRIGHT_FALLBACK_CODES
                or not playwright_worker._PLAYWRIGHT_FALLBACK_ENABLED
            ):
                record_failure(link, exc)
                if exc.code in {"anti_bot_challenge", "access_denied"}:
                    break
                continue
        try:
            body_text, title = playwright_worker._render_with_playwright(link)
        except PublicFetchError as exc:
            if context is not None:
                _remember_blocked_domain(context, link, exc)
            record_failure(link, exc)
            if exc.code in {"anti_bot_challenge", "access_denied"}:
                break
            continue
        try:
            pages.append(
                _build_evidence_page(
                    requested_url=link,
                    effective_url=link,
                    title=title,
                    visible_text=body_text,
                    status_code=200,
                )
            )
        except PublicFetchError as exc:
            if context is not None:
                _remember_blocked_domain(context, link, exc)
            record_failure(link, exc)
            if exc.code in {"anti_bot_challenge", "access_denied"}:
                break
            continue
    return pages


def _expand_official_campus_detail(
    url: str,
    page: FetchPublicJobPageOutput,
    *,
    context: Any,
    failure_sink: list[PublicJobPageFetchFailure] | None = None,
    failure_lock: threading.Lock | None = None,
) -> list[FetchPublicJobPageOutput]:
    """Boundedly inspect the official campus portal indexes behind one JD.

    Some university portals expose a complete JD at the supplied URL but do
    not link sibling postings from that page.  For the reviewed Hebut portal,
    two public, paginated index pages are the smallest deterministic route to
    recent postings; their same-host detail links are then expanded by the
    normal bounded list handler.  This is public-page fetching only: every
    derived URL is still validated, and failures remain explicit.
    """
    parsed = urlsplit(url)
    if (
        page.quality != "jd_complete"
        or (parsed.hostname or "").lower() != _CAMPUS_PORTAL_HOST
        or not parsed.path.lower().startswith("/correcruit/content/")
    ):
        return []
    expanded: list[FetchPublicJobPageOutput] = []
    seen: set[str] = {url}
    for page_number in range(1, _MAX_CAMPUS_INDEX_PAGES + 1):
        index_url = (
            f"https://{_CAMPUS_PORTAL_HOST}/correcruit/index.html?p={page_number}"
        )
        try:
            index_page, raw_html = _fetch_public_page_requests_with_html(index_url)
        except PublicFetchError as error:
            if context is not None:
                _remember_blocked_domain(context, index_url, error)
            if failure_sink is not None:
                _persist_fetch_failure(
                    failure_sink,
                    failure_lock,
                    source_url=index_url,
                    error=error,
                )
            continue
        if index_page.source_url not in seen:
            expanded.append(index_page)
            seen.add(index_page.source_url)
        collector = _HtmlLinkCollector(index_url)
        collector.feed(raw_html)
        details = _expand_from_list_links(
            index_url,
            collector.links,
            index_page.visible_text,
            context=context,
            failure_sink=failure_sink,
            failure_lock=failure_lock,
            max_links=_MAX_CAMPUS_DETAIL_PAGES,
        )
        for detail in details:
            if detail.source_url not in seen:
                expanded.append(detail)
                seen.add(detail.source_url)
    return expanded


def _fetch_one_with_expansion(
    context: Any,
    url: str,
    *,
    failure_sink: list[PublicJobPageFetchFailure] | None = None,
    failure_lock: threading.Lock | None = None,
) -> list[FetchPublicJobPageOutput]:
    """Fetch one URL with P2 list-page expansion, returning 1..N evidence pages.

    WeChat and adapter routes keep their single-page semantics (their bodies
    are already the terminal evidence).  Both the ``requests`` fast path and
    the render fallback can expand: the page is checked for card-list shape,
    and when it qualifies the detail pages behind its same-host job-shaped
    links are deep-fetched and appended after the list page itself.  On the
    requests path the raw HTML is scanned once for anchors (A2, RC-B), so
    server-rendered card lists (e.g. liepin) expand deterministically without
    a browser; the render path collects links from the rendered DOM.
    """
    _assert_public_url(url)
    _ensure_domain_available(context, url)
    cached_page = _govern_get_cached_page(context, url)
    if cached_page is not None:
        return [cached_page]
    _govern_before_request(context, url)
    wechat_page = _fetch_wechat_article_page(context, url)
    if wechat_page is not None:
        _govern_put_cached_page(context, wechat_page)
        return [wechat_page]
    adapter_page = _fetch_via_adapter(url)
    if adapter_page is not None:
        _govern_put_cached_page(context, adapter_page)
        return [adapter_page]
    try:
        page, raw_html = _fetch_public_page_requests_with_html(url)
    except PublicFetchError as error:
        _remember_blocked_domain(context, url, error)
        if (
            error.code not in playwright_worker._PLAYWRIGHT_FALLBACK_CODES
            or not playwright_worker._PLAYWRIGHT_FALLBACK_ENABLED
        ):
            raise
    else:
        _govern_put_cached_page(context, page)
        collector = _HtmlLinkCollector(url)
        collector.feed(raw_html)
        tencent_links = _tencent_query_detail_urls(url, raw_html)
        if tencent_links:
            return [
                page,
                *_expand_from_list_links(
                    url,
                    tencent_links,
                    page.visible_text,
                    context=context,
                    failure_sink=failure_sink,
                    failure_lock=failure_lock,
                ),
            ]
        try:
            iguopin_links = _iguopin_list_detail_urls(url)
        except PublicFetchError as probe_error:
            if failure_sink is not None:
                _persist_fetch_failure(
                    failure_sink,
                    failure_lock,
                    source_url=url,
                    error=probe_error,
                )
            return [page]
        if iguopin_links:
            return [
                page,
                *_expand_from_list_links(
                    url,
                    iguopin_links,
                    page.visible_text,
                    context=context,
                    failure_sink=failure_sink,
                    failure_lock=failure_lock,
                ),
            ]
        campus_pages = _expand_official_campus_detail(
            url,
            page,
            context=context,
            failure_sink=failure_sink,
            failure_lock=failure_lock,
        )
        if campus_pages:
            return [page, *campus_pages]
        return [
            page,
            *_expand_from_list_links(
                url,
                collector.links,
                page.visible_text,
                context=context,
                failure_sink=failure_sink,
                failure_lock=failure_lock,
            ),
        ]
    rendered_text, rendered_title, links = playwright_worker._render_with_playwright(
        url, collect_links=True
    )
    rendered_effective_url, rendered_status = playwright_worker._render_metadata(url)
    try:
        list_page = _build_evidence_page(
            requested_url=url,
            effective_url=rendered_effective_url,
            title=rendered_title,
            visible_text=rendered_text,
            status_code=rendered_status,
        )
        _govern_put_cached_page(context, list_page)
    except PublicFetchError as error:
        _remember_blocked_domain(context, url, error)
        raise
    try:
        iguopin_links = _iguopin_list_detail_urls(url)
    except PublicFetchError as probe_error:
        if failure_sink is not None:
            _persist_fetch_failure(
                failure_sink,
                failure_lock,
                source_url=url,
                error=probe_error,
            )
        return [list_page]
    if iguopin_links:
        return [
            list_page,
            *_expand_from_list_links(
                url,
                iguopin_links,
                list_page.visible_text,
                context=context,
                failure_sink=failure_sink,
                failure_lock=failure_lock,
            ),
        ]
    campus_pages = _expand_official_campus_detail(
        url,
        list_page,
        context=context,
        failure_sink=failure_sink,
        failure_lock=failure_lock,
    )
    if campus_pages:
        return [list_page, *campus_pages]
    return [
        list_page,
        *_expand_from_list_links(
            url,
            links,
            list_page.visible_text,
            context=context,
            failure_sink=failure_sink,
            failure_lock=failure_lock,
        ),
    ]


def fetch_public_job_pages(
    context: Any, payload: FetchPublicJobPagesInput
) -> FetchPublicJobPagesOutput:
    """Capture a bounded candidate set without hiding individual public-page errors.

    Fetches run with bounded concurrency (C5): deterministic input-index
    ordering, i/n progress lines, and per-item error isolation identical to
    the sequential loop this replaced.  A rendered card-list page (JS SPA,
    no JD body, job-shaped detail links) is expanded in place (P2): the list
    page stays first in ``pages`` and up to ``_MAX_LIST_EXPANSION`` detail
    pages follow it, so later extract/match tools see real JD body.
    """
    pages: list[FetchPublicJobPageOutput] = []
    failures: list[PublicJobPageFetchFailure] = []
    failure_lock = threading.Lock()

    def work(url: str) -> list[FetchPublicJobPageOutput]:
        return _fetch_one_with_expansion(
            context,
            url,
            failure_sink=failures,
            failure_lock=failure_lock,
        )

    # Cross-call URL dedup: the kernel seeds fetched_job_urls as a
    # step-scoped mutable list (projected by reference into
    # ToolContext.metadata), so a URL already captured earlier in this run is
    # reported as duplicate_job_url instead of being re-fetched (wasted model
    # tokens + page flood).  Without the shared list (direct tool tests /
    # recovery path) the batch runs untouched.  Batch-internal duplicates are
    # already rejected by the input validator.
    shared_fetched = context.metadata.get("fetched_job_urls")
    if isinstance(shared_fetched, list):
        seen = set(shared_fetched)
        duplicate_urls = [url for url in payload.urls if url in seen]
        remaining = [url for url in payload.urls if url not in seen]
        shared_fetched.extend(remaining)
        if duplicate_urls:
            for url in duplicate_urls:
                failures.append(
                    PublicJobPageFetchFailure(
                        source_url=url,
                        error_code="duplicate_job_url",
                        message=(
                            "该 URL 已在本次运行中抓取过，证据已保存；请直接使用已有证据，"
                            "不要重复抓取相同页面。"
                        ),
                    )
                )
        payload = FetchPublicJobPagesInput(urls=remaining) if remaining else None
        if payload is None or not remaining:
            return FetchPublicJobPagesOutput(pages=[], failures=failures)

    # Production runs deliberately serialize the batch.  Parallel requests
    # to one recruiting host are a common, avoidable trigger for access
    # controls; direct unit callers retain the historical bounded parallel
    # runner unless the controller enables the governor.
    workers = 1 if context.metadata.get("enforce_public_request_governor") else 4
    batch = run_parallel_with_progress(
        payload.urls,
        work,
        workers=workers,
        label="url",
        key=lambda url: url,
    )
    for result in batch:
        if result.error is not None:
            error = result.error
            code = error.code if isinstance(error, PublicFetchError) else "public_fetch_failed"
            message = str(error)
            if isinstance(error, PublicFetchError) and code in {
                "anti_bot_challenge",
                "access_denied",
                "domain_temporarily_blocked",
            }:
                # Give the model a concrete next step instead of a bare block:
                # anti-bot on one aggregator must not end discovery -- the
                # Executor should switch to search-public-job-pages for
                # campus/static-JD sources (career.*.edu.cn, WatchJobs etc.).
                message = (
                    f"{message}。该源反爬或已被暂禁，请改用 search-public-job-pages "
                    "搜索校园招聘站或静态 JD 聚合源（如 career.*.edu.cn、job.*.edu.cn、WatchJobs 等），"
                    "不要就此停止或仅向用户索取链接。"
                )
            failures.append(
                PublicJobPageFetchFailure(
                    source_url=result.item,
                    error_code=code,
                    effective_url=(
                        error.effective_url if isinstance(error, PublicFetchError) else None
                    ),
                    redirect_chain=(
                        error.redirect_chain if isinstance(error, PublicFetchError) else []
                    ),
                    http_status=(
                        error.status_code if isinstance(error, PublicFetchError) else None
                    ),
                    message=message,
                )
            )
        elif result.value is not None:
            pages.extend(result.value)
    return FetchPublicJobPagesOutput(pages=pages, failures=failures)


__all__ = [
    "_MIN_LIST_LINKS",
    "_MAX_LIST_EXPANSION",
    "_CAMPUS_PORTAL_HOST",
    "_MAX_CAMPUS_INDEX_PAGES",
    "_MAX_CAMPUS_DETAIL_PAGES",
    "_iguopin_list_detail_urls",
    "_tencent_query_detail_urls",
    "_persist_fetch_failure",
    "_expand_from_list_links",
    "_expand_official_campus_detail",
    "_fetch_one_with_expansion",
    "fetch_public_job_pages",
]
