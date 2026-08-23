"""Same-host job-shaped link collection + detail-link prioritization.

Verbatim ports from ``skill/job_discovery/runtime/job_discovery.py``:

- ``_JOB_RESULT_URL_TOKENS`` / ``_JOB_RESULT_TEXT_RE`` (347-364);
- ``_prioritize_detail_links`` (110-149) and ``_prioritize_direct_search_results``
  (152-168);
- ``_collect_page_links`` (1958-2003) -- rendered-DOM link collection;
- ``_same_host_or_linkedin_public_detail`` (2006-2037) -- the one audited
  cross-subdomain route LinkedIn's guest search API may emit;
- ``_HtmlLinkCollector`` (2040-2105) -- the same filters on raw HTML.

Every candidate URL passes ``_is_public_url`` (DNS-resolving) before it is
kept, so list-page expansion can never follow a private / cloud-metadata
address.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

from .url_guard import _is_public_url

_JOB_RESULT_URL_TOKENS = (
    "career",
    "job",
    "jobs",
    "talent",
    "recruit",
    "zhaopin",
    "position",
    "campus",
    "greenhouse",
    "lever.co",
    "workday",
)
_JOB_RESULT_TEXT_RE = re.compile(
    r"(?:招聘|职位|岗位|校招|社招|实习|工程师|开发|算法|researcher|"
    r"engineer|developer|intern|hiring|career|job)",
    re.IGNORECASE,
)


def _prioritize_detail_links(links: list[str]) -> list[str]:
    """Put likely detail routes before navigation links from the same page.

    Campus portals commonly expose navigation links (``/index.html``,
    ``/recruitment/index.html``) before the actual job links in their HTML.
    Expansion is intentionally bounded, so consuming the cap on navigation
    pages silently loses the public JD evidence that follows them.  The
    existing URL safety and same-host filters remain authoritative; this only
    changes the order of already-accepted links and preserves stable order for
    links with the same score.
    """
    detail_route = re.compile(
        r"/(?:content|detail|position|job|jobs|post)(?:/|$)", re.IGNORECASE
    )
    detail_query = re.compile(
        r"(?:^|_)(?:id|job[_-]?id|position[_-]?id|post[_-]?id)=",
        re.IGNORECASE,
    )

    def score(url: str) -> int:
        parsed = urlsplit(url)
        path = parsed.path.rstrip("/").lower()
        basename = path.rsplit("/", 1)[-1]
        value = 0
        if detail_route.search(path):
            value += 4
        if detail_query.search(parsed.query):
            value += 2
        if re.search(r"/(?:id|position|post)/[^/]+$", path):
            value += 1
        if basename in {"index", "index.html", "list", "search", "careers"}:
            value -= 3
        return value

    return [
        url
        for _, url in sorted(
            enumerate(links), key=lambda item: (-score(item[1]), item[0])
        )
    ]


def _prioritize_direct_search_results(
    results: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Rank direct detail results ahead of list/search shells, stably."""
    urls = [item["url"] for item in results if isinstance(item.get("url"), str)]
    ordered_urls = _prioritize_detail_links(urls)
    ranks = {url: index for index, url in enumerate(ordered_urls)}
    return [
        item
        for _index, item in sorted(
            enumerate(results),
            key=lambda pair: (
                ranks.get(str(pair[1].get("url")), len(ranks)),
                pair[0],
            ),
        )
    ]


def _collect_page_links(page: Any, origin_url: str) -> list[str]:
    """Same-host job-shaped ``<a href>`` targets from a rendered DOM.

    Only http(s) targets that pass the public-URL checks and share the
    origin's hostname are kept, so list-page expansion never follows a
    cross-host redirect ladder or a private/cloud-metadata address.  The
    path filter reuses the search-result URL tokens (career/job/position/
    campus/...), which match the detail-route shapes of the campus-portal
    SPA family the expansion targets.
    """
    try:
        raw = page.eval_on_selector_all(
            "a[href], [data-href], [data-url], [data-link], [data-detail-url]",
            """els => els.flatMap(e => [
                e.href,
                e.getAttribute('data-href'),
                e.getAttribute('data-url'),
                e.getAttribute('data-link'),
                e.getAttribute('data-detail-url')
            ]).filter(Boolean)""",
        )
    except Exception:
        return []
    origin_host = urlsplit(origin_url).hostname
    seen: set[str] = set()
    links: list[str] = []
    for href in raw:
        if not isinstance(href, str):
            continue
        href = urljoin(origin_url, href)
        if not href.startswith(("http://", "https://")):
            continue
        if not _is_public_url(href):
            continue
        if not _same_host_or_linkedin_public_detail(
            origin_url, href, origin_host=origin_host
        ):
            continue
        path = urlsplit(href).path.lower()
        if not any(token in path for token in _JOB_RESULT_URL_TOKENS):
            continue
        if href in seen:
            continue
        seen.add(href)
        links.append(href)
    return links


def _same_host_or_linkedin_public_detail(
    origin_url: str,
    target_url: str,
    *,
    origin_host: str | None = None,
) -> bool:
    """Allow one audited cross-subdomain list route used by LinkedIn.

    LinkedIn's anonymous job-search endpoint lives on ``www.linkedin.com``
    while its public details are localized to hosts such as
    ``cn.linkedin.com``.  This exception is deliberately narrower than an
    eTLD+1 match: only the guest search API may emit it, and only
    ``/jobs/view/`` details on a real LinkedIn subdomain are accepted.
    """
    origin = urlsplit(origin_url)
    target = urlsplit(target_url)
    effective_origin_host = origin_host or origin.hostname
    if target.hostname == effective_origin_host:
        return True
    origin_hostname = (effective_origin_host or "").lower().rstrip(".")
    target_hostname = (target.hostname or "").lower().rstrip(".")
    return (
        origin_hostname == "www.linkedin.com"
        and origin.path.startswith(
            "/jobs-guest/jobs/api/seeMoreJobPostings/search"
        )
        and (
            target_hostname == "linkedin.com"
            or target_hostname.endswith(".linkedin.com")
        )
        and target.path.startswith("/jobs/view/")
    )


class _HtmlLinkCollector(HTMLParser):
    """Same-host job-shaped ``<a href>`` targets from raw HTML.

    Mirrors the filters of ``_collect_page_links`` (rendered DOM) on the
    requests fast path: only http(s) targets that pass the public-URL checks
    and share the origin's hostname are kept, the path filter reuses the
    search-result URL tokens (career/job/position/campus/...), relative hrefs
    are resolved against the page URL with ``urljoin``, and duplicates are
    dropped. Anchors inside ``script``/``style``/``noscript`` blocks are
    skipped (mirroring ``_VisibleTextParser``), so inline JSON never leaks
    random URLs into the card-list candidate set.
    """

    _IGNORED = {"script", "style", "noscript"}

    def __init__(self, origin_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._origin_url = origin_url
        self._origin_host = urlsplit(origin_url).hostname
        self._ignored_depth = 0
        self._seen: set[str] = set()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self._IGNORED:
            self._ignored_depth += 1
        if tag != "a" or self._ignored_depth:
            return
        attrs_map = dict(attrs)
        href = attrs_map.get("href")
        data_candidates = [
            attrs_map.get(key)
            for key in ("data-href", "data-url", "data-link", "data-detail-url")
        ]
        if not isinstance(href, str) or not href:
            href = next((value for value in data_candidates if isinstance(value, str) and value), None)
        if not isinstance(href, str) or not href:
            return
        candidates = [href]
        for value in data_candidates:
            if isinstance(value, str) and value:
                candidates.append(value)
        for candidate in candidates:
            self._add_candidate(candidate)

    def _add_candidate(self, href: str) -> None:
        resolved = urljoin(self._origin_url, href)
        if not resolved.startswith(("http://", "https://")):
            return
        if not _is_public_url(resolved):
            return
        if not _same_host_or_linkedin_public_detail(
            self._origin_url, resolved, origin_host=self._origin_host
        ):
            return
        path = urlsplit(resolved).path.lower()
        if not any(token in path for token in _JOB_RESULT_URL_TOKENS):
            return
        if resolved in self._seen:
            return
        self._seen.add(resolved)
        self.links.append(resolved)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORED and self._ignored_depth:
            self._ignored_depth -= 1


__all__ = [
    "_JOB_RESULT_URL_TOKENS",
    "_JOB_RESULT_TEXT_RE",
    "_prioritize_detail_links",
    "_prioritize_direct_search_results",
    "_collect_page_links",
    "_same_host_or_linkedin_public_detail",
    "_HtmlLinkCollector",
]
