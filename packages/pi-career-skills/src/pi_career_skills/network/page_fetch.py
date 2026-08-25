"""SSRF-guarded public page fetch: requests fast path + evidence contract.

Verbatim ports from ``skill/job_discovery/runtime/job_discovery.py``:

- ``HttpFetchResult`` (480-495) and ``_VisibleTextParser`` (948-976);
- ``_detect_access_block`` (1332-1356) and the domain circuit
  (``_domain_scope`` / ``_blocked_domains`` / ``_ensure_domain_available`` /
  ``_remember_blocked_domain``, 1359-1394);
- ``_fetch_validated`` (1397-1479) -- manual redirect walking, every
  ``Location`` hop re-validated against the public-URL guard, one transport
  retry, ``PublicFetchError`` (source PublicJobFetchError) never retried;
- ``fetch_public_job_page`` (1763-1817) -- WeChat OCR route, adapter route,
  requests fast path, Playwright render fallback;
- ``_dead_link_code`` / ``_normalize_visible_text`` (1819-1839) and
  ``_build_evidence_page`` (1842-1907) -- the one classification /
  normalization contract every fetch path runs through;
- ``_fetch_public_page_requests_with_html`` / ``_fetch_public_page_requests``
  (1910-1955).

Classification helpers come from the pi ``business.job_discovery.models``
(byte-identical to source per the §6 contract snapshot gate).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

from ..business.job_discovery.models import (
    _JD_MARKER_SCAN_HEAD_CHARS,
    _MIN_REAL_JD_TEXT_CHARS,
    _MIN_USABLE_TEXT_CHARS,
    FetchPublicJobPageInput,
    FetchPublicJobPageOutput,
    _classify_page_quality_with_url,
)
from . import playwright_worker
from .adapters import _fetch_via_adapter
from .page_links import _HtmlLinkCollector
from .request_governor import (
    before_request as _govern_before_request,
)
from .request_governor import (
    ensure_available as _govern_ensure_available,
)
from .request_governor import (
    get_cached_page as _govern_get_cached_page,
)
from .request_governor import (
    put_cached_page as _govern_put_cached_page,
)
from .request_governor import (
    remember_blocked as _govern_remember_blocked,
)
from .url_guard import PublicFetchError, _assert_public_url
from .wechat import _fetch_wechat_article_page

_MAX_PUBLIC_REDIRECTS = 5
_PUBLIC_FETCH_HEADERS = {"User-Agent": "CareerAssistantPEV/1.0 (+public-job-fetch)"}

# Soft-404 markers (W2): a page that carries one of these strings in its title
# or its first ``_JD_MARKER_SCAN_HEAD_CHARS`` body chars, with almost no JD
# body, is a dead link rather than valid evidence. Classified as ``dead_link``
# -- a neutral failure, NOT a blocked code -- so it feeds the
# search-authorization rule (search allowed only after EVERY candidate URL
# failed) without ever entering needs_manual_review.
_SOFT_404_MARKERS = ("页面不存在", "职位已下线", "职位不存在", "页面已经过期")
# Upper bound for the visible-text evidence captured from one page. The Feishu
# campus portal renders a whole 100-job listing (with inline JD sections) in a
# single DOM pass (~26k chars for the 61 NIO agent roles), so the cap must
# comfortably exceed the largest single-portal render while staying far under
# the 48k per-run evidence budget kept full for recent artifacts.
_MAX_VISIBLE_TEXT_CHARS = 32_000
# List/shell pages are routing evidence: the model only needs their
# detail_links, not the whole page text.  Trimming the returned visible_text
# stops a batch of list pages from flooding the model context (regression: a
# model fetching a dozen search pages burned the whole input-token budget
# while jd_complete pages keep their full text for extraction).
_LIST_PAGE_VISIBLE_TEXT_CHARS = 4_000
_ACCESS_BLOCK_TEXT_MARKERS = (
    "安全验证",
    "验证码",
    "访问验证",
    "captcha",
    "verify you are human",
    "security center",
    "安全中心",
    "人机验证",
)


@dataclass(frozen=True)
class HttpFetchResult:
    """Validated HTTP response plus redirect provenance.

    ``__getattr__`` keeps the old internal response-shaped contract working
    for callers that only need ``status_code``, ``content`` or
    ``raise_for_status`` while making the provenance explicit to new code.
    """

    response: Any
    requested_url: str
    effective_url: str
    redirect_chain: list[str]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.response, name)


class _VisibleTextParser(HTMLParser):
    _IGNORED = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self._IGNORED:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORED and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        normalized = " ".join(data.split())
        if not normalized or self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(normalized)
            return
        self.text_parts.append(normalized)


def _detect_access_block(
    *,
    effective_url: str,
    title: str | None,
    visible_text: str,
    status_code: int | None,
) -> str | None:
    """Classify access gates before generic empty/short-page heuristics."""
    parsed = urlsplit(effective_url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if host == "safe.liepin.com" and (
        "captcha" in path or "security" in path or "verify" in path
    ):
        return "anti_bot_challenge"
    if host == "wow.liepin.com" and "transit" in path:
        return "anti_bot_challenge"
    text = f"{title or ''}\n{visible_text[:2_000]}".lower()
    if any(marker.lower() in text for marker in _ACCESS_BLOCK_TEXT_MARKERS):
        return "anti_bot_challenge"
    if status_code == 429:
        return "rate_limited"
    if status_code in {401, 403}:
        return "access_denied"
    return None


def _domain_scope(url: str) -> str:
    """Return the run-level circuit key without merging unrelated registries."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host == "liepin.com" or host.endswith(".liepin.com"):
        return "liepin.com"
    return host


def _blocked_domains(context: Any) -> set[str]:
    raw = context.metadata.get("blocked_public_domains", [])
    if not isinstance(raw, list):
        return set()
    return {item for item in raw if isinstance(item, str) and item}


def _ensure_domain_available(context: Any, url: str) -> None:
    _govern_ensure_available(context, url)
    domain = _domain_scope(url)
    if domain in _blocked_domains(context):
        raise PublicFetchError(
            "domain_temporarily_blocked",
            message=f"当前运行已暂停访问 {domain}：该域名此前返回了反爬或访问阻断。",
            effective_url=url,
        )


def _remember_blocked_domain(
    context: Any, url: str, error: PublicFetchError
) -> None:
    if error.code not in {"anti_bot_challenge", "access_denied"}:
        return
    domains = _blocked_domains(context)
    domains.add(_domain_scope(url))
    context.metadata["blocked_public_domains"] = sorted(domains)
    context.metadata.setdefault("blocked_public_domain_reasons", {})[
        _domain_scope(url)
    ] = error.code
    _govern_remember_blocked(context, url, error.code)


def _fetch_validated(url: str) -> HttpFetchResult:
    """GET ``url`` following redirects manually, re-checking every hop is public.

    ``requests`` follows 3xx redirects automatically, but it never re-runs
    ``_assert_public_url`` against the ``Location`` target -- so a public URL
    that redirects to an internal address (loopback, link-local, RFC1918, or a
    cloud metadata endpoint) would let an Agent read private network state. We
    disable auto-redirect and walk each hop ourselves, re-validating the scheme,
    absence of userinfo, and global-IP rule on every resolved target.

    One transport-level retry is performed before a timeout/connection failure
    is handed back: a single transient failure (flaky CDN edge, connection
    reset) should not waste a whole Executor turn on a URL that succeeds a
    moment later. Only ``requests.RequestException`` (timeout / connection /
    HTTP transport errors) is retried; a ``PublicFetchError`` from the
    redirect-walk (``unsafe_public_url``) or any blocked/4xx outcome stays
    final -- retrying a security rejection would be both useless and risky.
    """
    current = url
    redirect_chain = [url]
    last_status: int | None = None
    attempt = 0
    while True:
        try:
            for _ in range(_MAX_PUBLIC_REDIRECTS + 1):
                response = requests.get(
                    current,
                    timeout=20,
                    allow_redirects=False,
                    headers=_PUBLIC_FETCH_HEADERS,
                )
                last_status = getattr(response, "status_code", None)
                if not response.is_redirect:
                    return HttpFetchResult(
                        response=response,
                        requested_url=url,
                        effective_url=current,
                        redirect_chain=list(redirect_chain),
                    )
                target = response.headers.get("Location")
                if not target:
                    return HttpFetchResult(
                        response=response,
                        requested_url=url,
                        effective_url=current,
                        redirect_chain=list(redirect_chain),
                    )
                target = urljoin(current, target)
                try:
                    _assert_public_url(target)
                except PublicFetchError as exc:
                    raise PublicFetchError(
                        exc.code,
                        str(exc),
                        effective_url=target,
                        redirect_chain=[*redirect_chain, target],
                        status_code=last_status,
                    ) from exc
                current = target
                redirect_chain.append(current)
            raise PublicFetchError(
                "unsafe_public_url",
                effective_url=current,
                redirect_chain=redirect_chain,
                status_code=last_status,
            )
        except PublicFetchError as exc:
            if not exc.effective_url:
                raise PublicFetchError(
                    exc.code,
                    str(exc),
                    effective_url=current,
                    redirect_chain=redirect_chain,
                    status_code=last_status,
                ) from exc
            raise
        except requests.RequestException:
            if attempt >= 1:
                raise
            attempt += 1
            # restart the whole redirect walk from the original URL
            current = url


def fetch_public_job_page(
    context: Any, payload: FetchPublicJobPageInput
) -> FetchPublicJobPageOutput:
    """Fetch public evidence and expose immutable visible-text evidence.

    A WeChat article URL (``mp.weixin.qq.com``) is OCR-routed first: its body
    is image content the generic chain reads as an empty shell. A URL covered
    by a certified A1 adapter (moka/beisen/didi/netease/baidu) is fetched
    adapter-first when the channel is enabled: the adapter is the
    authoritative channel for its hosts, so a covered URL that fails is a
    hard ``adapter:<code>`` blocked error, never a silent fallthrough.
    Uncovered URLs take the plain ``requests`` fast path; when that fails or
    returns a shell with no usable text (SPA / login wall), the fetch falls
    back to a headless-Chromium render of the same URL -- still under the
    original public-URL validation. The fallbacks are gated by runtime flags
    so unit suites stay deterministic.
    """
    _assert_public_url(payload.url)
    _ensure_domain_available(context, payload.url)
    cached_page = _govern_get_cached_page(context, payload.url)
    if cached_page is not None:
        return cached_page
    _govern_before_request(context, payload.url)
    # Record this requested URL in the step-scoped fetched set so a later
    # fetch (single or batch) reports it as duplicate instead of re-fetching.
    shared_fetched = context.metadata.get("fetched_job_urls")
    if isinstance(shared_fetched, list) and payload.url not in shared_fetched:
        shared_fetched.append(payload.url)
    wechat_page = _fetch_wechat_article_page(context, payload.url)
    if wechat_page is not None:
        _govern_put_cached_page(context, wechat_page)
        return wechat_page
    adapter_page = _fetch_via_adapter(payload.url)
    if adapter_page is not None:
        _govern_put_cached_page(context, adapter_page)
        return adapter_page
    try:
        page = _fetch_public_page_requests(payload.url)
        _govern_put_cached_page(context, page)
        return page
    except PublicFetchError as error:
        _remember_blocked_domain(context, payload.url, error)
        if (
            error.code not in playwright_worker._PLAYWRIGHT_FALLBACK_CODES
            or not playwright_worker._PLAYWRIGHT_FALLBACK_ENABLED
        ):
            raise
    rendered = playwright_worker._render_with_playwright(payload.url, collect_links=True)
    rendered_text, rendered_title = rendered[:2]
    rendered_links = list(rendered[2]) if len(rendered) >= 3 else []
    rendered_effective_url, rendered_status = playwright_worker._render_metadata(payload.url)
    try:
        page = _build_evidence_page(
            requested_url=payload.url,
            effective_url=rendered_effective_url,
            title=rendered_title,
            visible_text=rendered_text,
            status_code=rendered_status,
            detail_links=rendered_links,
        )
        _govern_put_cached_page(context, page)
        return page
    except PublicFetchError as error:
        _remember_blocked_domain(context, payload.url, error)
        raise


def _dead_link_code(text: str, title: str | None) -> str | None:
    """Return ``dead_link`` when the page is a soft-404, else None (W2).

    A page with at least ``_MIN_REAL_JD_TEXT_CHARS`` of usable text is real
    content even if a marker string appears somewhere: markers are only
    scanned in the title and the first ``_JD_MARKER_SCAN_HEAD_CHARS`` chars of
    the body, and a "404" in the title also classifies the page as dead.
    """
    if len(text) >= _MIN_REAL_JD_TEXT_CHARS:
        return None
    if any(marker in text[:_JD_MARKER_SCAN_HEAD_CHARS] for marker in _SOFT_404_MARKERS):
        return "dead_link"
    if title and ("404" in title or any(marker in title for marker in _SOFT_404_MARKERS)):
        return "dead_link"
    return None


def _normalize_visible_text(text: str) -> str:
    """Normalize evidence text so requests and rendered paths hash alike."""
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _build_evidence_page(
    *,
    requested_url: str,
    effective_url: str,
    title: str | None,
    visible_text: str,
    status_code: int | None,
    redirect_chain: list[str] | None = None,
    detail_links: list[str] | None = None,
) -> FetchPublicJobPageOutput:
    """Apply one classification/normalization contract to every fetch path."""
    normalized_text = _normalize_visible_text(visible_text)
    blocked_code = _detect_access_block(
        effective_url=effective_url,
        title=title,
        visible_text=normalized_text,
        status_code=status_code,
    )
    diagnostics = {
        "effective_url": effective_url,
        "redirect_chain": list(redirect_chain or [requested_url]),
        "status_code": status_code,
    }
    if blocked_code is not None:
        raise PublicFetchError(
            blocked_code,
            message=(
                f"公开页面被站点访问控制阻断（effective_url={effective_url}, "
                f"status={status_code}）。不会继续无状态重试。"
            ),
            **diagnostics,
        )
    if not normalized_text:
        raise PublicFetchError("empty_public_page", **diagnostics)
    dead_code = _dead_link_code(normalized_text, title)
    if dead_code is not None:
        raise PublicFetchError(
            dead_code,
            message="页面已下线或不存在（死链），非有效岗位证据。",
            **diagnostics,
        )
    if len(normalized_text) < _MIN_USABLE_TEXT_CHARS:
        raise PublicFetchError("public_page_content_insufficient", **diagnostics)
    content_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
    # List pages are routing evidence; trim the text the model sees while
    # keeping the full-text hash for content-addressing/dedup.  The quality is
    # pinned so the output validator does not re-classify the trimmed text.
    quality, quality_signal = _classify_page_quality_with_url(
        normalized_text, url=effective_url or requested_url
    )
    visible_text_out = normalized_text[:_MAX_VISIBLE_TEXT_CHARS]
    if (
        quality in {"list_only", "js_shell", "empty"}
        and len(visible_text_out) > _LIST_PAGE_VISIBLE_TEXT_CHARS
    ):
        visible_text_out = visible_text_out[:_LIST_PAGE_VISIBLE_TEXT_CHARS]
    return FetchPublicJobPageOutput(
        artifact_id=f"observed:{content_hash}",
        source_url=requested_url,
        effective_url=effective_url,
        redirect_chain=list(redirect_chain or [requested_url]),
        http_status=status_code,
        title=title,
        visible_text=visible_text_out,
        content_hash=content_hash,
        detail_links=list(detail_links or []),
        quality=quality,
        quality_signal=quality_signal,
    )


def _fetch_public_page_requests_with_html(
    url: str,
) -> tuple[FetchPublicJobPageOutput, str]:
    """The non-rendered evidence path: requests + visible-text normalization.

    Returns ``(page_evidence, raw_html)``; the raw HTML lets the requests
    fast path run the same card-list expansion as the render path (A2, RC-B)
    without a second fetch. Backward-compatible single-value callers keep
    using ``_fetch_public_page_requests``.
    """
    try:
        fetched = _fetch_validated(url)
        response = fetched.response
        effective_url = fetched.effective_url
        redirect_chain = fetched.redirect_chain
        status_code = getattr(response, "status_code", None)
        if status_code not in {401, 403, 429}:
            response.raise_for_status()
    except PublicFetchError:
        raise
    except requests.RequestException as exc:
        raise PublicFetchError("public_fetch_failed") from exc
    if (
        response.encoding is None
        or response.encoding.lower() in {"iso-8859-1", "latin-1"}
    ):
        response.encoding = response.apparent_encoding or "utf-8"
    html = response.text
    parser = _VisibleTextParser()
    parser.feed(html)
    title = " ".join(parser.title_parts) or None
    collector = _HtmlLinkCollector(url)
    collector.feed(html)
    page = _build_evidence_page(
        requested_url=url,
        effective_url=effective_url,
        title=title,
        visible_text="\n".join(parser.text_parts),
        status_code=status_code,
        redirect_chain=redirect_chain,
        detail_links=collector.links,
    )
    return page, html


def _fetch_public_page_requests(url: str) -> FetchPublicJobPageOutput:
    """Backward-compatible wrapper: the requests fast path, evidence only."""
    page, _html = _fetch_public_page_requests_with_html(url)
    return page


__all__ = [
    "_MAX_PUBLIC_REDIRECTS",
    "_PUBLIC_FETCH_HEADERS",
    "_SOFT_404_MARKERS",
    "_MAX_VISIBLE_TEXT_CHARS",
    "_LIST_PAGE_VISIBLE_TEXT_CHARS",
    "_ACCESS_BLOCK_TEXT_MARKERS",
    "HttpFetchResult",
    "_VisibleTextParser",
    "_detect_access_block",
    "_domain_scope",
    "_blocked_domains",
    "_ensure_domain_available",
    "_remember_blocked_domain",
    "_fetch_validated",
    "fetch_public_job_page",
    "_dead_link_code",
    "_normalize_visible_text",
    "_build_evidence_page",
    "_fetch_public_page_requests_with_html",
    "_fetch_public_page_requests",
]
