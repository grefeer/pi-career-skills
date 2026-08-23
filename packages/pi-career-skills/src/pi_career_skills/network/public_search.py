"""Public job-page search: Bing -> 360 -> Sogou fallback chain + Juejin official.

Verbatim ports from ``skill/job_discovery/runtime/job_discovery.py``:

- search constants and ``_normalize_search_query`` (370-447);
- ``_BingSearchResultParser`` / ``_SoSearchResultParser`` /
  ``_SogouMobileSearchResultParser`` (979-1111);
- ``search_public_job_pages`` (2750-2910) with the ``route_already_consumed``
  per-run route guard (``public_search_routes`` / ``public_search_query_hashes``
  metadata);
- ``_is_allowed_job_host`` (2913-2928), ``_is_plausible_public_job_result``
  (2931-3001), ``_direct_bing_result_url`` (3004-3024);
- the Juejin official recent-search path (2523-2747): named-source gate
  (task_goal must mention 稀土掘金/juejin + a 1-7 day window), ctime-filtered
  window, exact role/cohort hard constraints, scan evidence + content hash.

The route guard and Juejin gate make this deliberately narrow: a generic web
query must not silently become an exhaustive-source claim.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import uuid
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests

from ..business.job_discovery.models import (
    PublicCommunityScanRecord,
    PublicJobSearchResult,
    SearchPublicJobPagesInput,
    SearchPublicJobPagesOutput,
)
from ..context import ToolContext
from .page_fetch import _blocked_domains, _domain_scope, _fetch_validated
from .page_links import (
    _JOB_RESULT_TEXT_RE,
    _JOB_RESULT_URL_TOKENS,
    _prioritize_direct_search_results,
)
from .url_guard import PublicFetchError, _assert_public_url

# P2 (B4): known recruiting hosts pass on the loose URL-token OR text-signal
# check; any other host must carry a job-shaped URL path. This rejects the
# tutorial/encyclopedia noise that merely mentions 招聘/岗位 in its title.
# Patterns ending in "." match the first label (careers.example,
# career.hebut.edu.cn, jobs.bytedance.com); plain domains match by suffix.
_JOB_SEARCH_ALLOWED_HOST_PATTERNS = (
    "career.",
    "careers.",
    "jobs.",
    "campus.",
    "talent.",
    "recruit.",
    "hr.",
    "job.",
    "liepin.com",
    "iguopin.com",
    "zhaopin.com",
    "shixiseng.com",
    "lagou.com",
    "mokahr.com",
    "feishu.cn",
    "ncss.cn",
    "fenbi.com",
    "juejin.cn",
)
# site: operators appended to the Bing query (skipped when the agent already
# steers with "site:") so the provider itself biases toward recruiting domains
# instead of returning 教程/百科/官网首页 noise.
_JOB_SEARCH_SITE_OPERATORS = (
    "site:talent.baidu.com",
    "site:jobs.bytedance.com",
    "site:careers.tencent.com",
    "site:campus.tencent.com",
    "site:career.hebut.edu.cn",
    "site:fenbi.com",
    "site:juejin.cn",
    "site:liepin.com",
    "site:iguopin.com",
    "site:zhaopin.com",
    "site:shixiseng.com",
    "site:lagou.com",
    "site:job.ncss.cn",
)
# Matches a ``site:`` operator fragment inside a search-engine query, e.g.
# ``site:zhaopin.com`` or ``site:gov.cn``.
_SITE_OPERATOR_RE = re.compile(r"\bsite:[a-zA-Z0-9][a-zA-Z0-9.\-]*", re.IGNORECASE)

_MAX_PUBLIC_SEARCH_ROUTES = 3
_JUEJIN_SEARCH_API_URL = "https://api.juejin.cn/search_api/v1/search"
_JUEJIN_RECENT_SEARCH_QUERIES = ("招聘", "内推", "校招")
_JUEJIN_MAX_SEARCH_PAGES_PER_QUERY = 8


def _normalize_search_query(raw_query: str) -> str:
    """Merge every ``site:`` token in ``raw_query`` into one pure OR chain.

    The agent may steer discovery with its own ``site:`` fragment (e.g.
    ``site:zhaopin.com`` or ``site:gov.cn``). Naively appending
    ``" OR ".join(_JOB_SEARCH_SITE_OPERATORS)`` after such a query turns the
    agent's operator and the first campus operator into an implicit AND
    (``site:zhaopin.com site:talent.baidu.com``), which the engine resolves as
    *both must match* and starves discovery to empty or irrelevant results --
    the root cause of "search found nothing, then fabricated a table" runs.
    We instead strip every ``site:`` token out, merge it into the campus
    whitelist (the agent's own operators first, deduped), and join the whole
    set with a pure `` OR `` so every domain is an alternative, never a
    constraint. A query that becomes only ``site:`` fragments falls back to a
    bare 招聘 keyword so the engine still has something to fetch.
    """
    agent_sites = sorted(
        {m.group(0).lower() for m in _SITE_OPERATOR_RE.finditer(raw_query)},
        key=len,
        reverse=True,
    )
    cleaned = _SITE_OPERATOR_RE.sub(" ", raw_query)
    cleaned = " ".join(cleaned.split())
    merged: list[str] = []
    seen: set[str] = set()
    for operator in (*agent_sites, *_JOB_SEARCH_SITE_OPERATORS):
        key = operator.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(operator)
    if not merged:
        return cleaned
    base = cleaned if cleaned else "招聘"
    return f"{base} {' OR '.join(merged)}"


class _BingSearchResultParser(HTMLParser):
    """Small HTML parser for direct links in Bing's public result cards."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, list[str] | str] | None = None
        self._in_heading = False
        self._in_snippet = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        attributes = dict(attrs)
        if tag == "li" and self._current is None and "b_algo" in attributes.get("class", ""):
            self._current = {"title": [], "snippet": []}
            return
        if self._current is None:
            return
        if tag == "h2":
            self._in_heading = True
        elif tag == "p":
            self._in_snippet = True
        elif tag == "a" and self._in_heading:
            href = attributes.get("href")
            if href:
                self._current["url"] = href

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        if tag == "h2":
            self._in_heading = False
        elif tag == "p":
            self._in_snippet = False
        elif tag == "li":
            title = " ".join(self._current.get("title", []))
            url = self._current.get("url")
            snippet = " ".join(self._current.get("snippet", []))
            if isinstance(url, str) and title:
                item = {"title": title, "url": url}
                if snippet:
                    item["snippet"] = snippet
                self.results.append(item)
            self._current = None
            self._in_heading = False
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        normalized = " ".join(data.split())
        if not normalized:
            return
        if self._in_heading:
            title_parts = self._current["title"]
            assert isinstance(title_parts, list)
            title_parts.append(normalized)
        elif self._in_snippet:
            snippet_parts = self._current["snippet"]
            assert isinstance(snippet_parts, list)
            snippet_parts.append(normalized)


class _SoSearchResultParser(HTMLParser):
    """Read 360's public result anchors that expose their direct target URL."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._url: str | None = None
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag != "a" or self._url is not None:
            return
        direct_url = dict(attrs).get("data-mdurl")
        if isinstance(direct_url, str) and direct_url:
            self._url = direct_url
            self._title_parts = []

    def handle_data(self, data: str) -> None:
        if self._url is None:
            return
        normalized = " ".join(data.split())
        if normalized:
            self._title_parts.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._url is None:
            return
        title = " ".join(self._title_parts)
        if title:
            self.results.append({"title": title, "url": self._url})
        self._url = None
        self._title_parts = []


class _SogouMobileSearchResultParser(HTMLParser):
    """Read direct targets embedded in Sogou mobile result-wrapper URLs."""

    def __init__(self, source_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._source_url = source_url
        self.results: list[dict[str, str]] = []
        self._url: str | None = None
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag != "a" or self._url is not None:
            return
        href = dict(attrs).get("href")
        if not isinstance(href, str) or not href:
            return
        wrapper = urlsplit(urljoin(self._source_url, href))
        direct_url = parse_qs(wrapper.query).get("url", [None])[0]
        if isinstance(direct_url, str) and direct_url.startswith(("http://", "https://")):
            self._url = direct_url
            self._title_parts = []

    def handle_data(self, data: str) -> None:
        if self._url is None:
            return
        normalized = " ".join(data.split())
        if normalized:
            self._title_parts.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._url is None:
            return
        title = " ".join(self._title_parts)
        if title:
            self.results.append({"title": title, "url": self._url})
        self._url = None
        self._title_parts = []


def _juejin_recent_days(context: ToolContext) -> int | None:
    """Return the explicit recent window for a named Juejin source request.

    The official adapter is deliberately narrow: a generic web query must not
    silently become an exhaustive-source claim. Juejin must be named in the
    original task goal and the goal must state a window supported by its
    public search period filter (at most seven days).
    """
    task_goal = context.metadata.get("task_goal")
    if not isinstance(task_goal, str) or not any(
        marker in task_goal.lower() for marker in ("稀土掘金", "juejin")
    ):
        return None
    match = re.search(r"(?:最近|近|过去|过去的)\s*(\d+)\s*(?:天|日)", task_goal)
    if match is None:
        return None
    recent_days = int(match.group(1))
    return recent_days if 1 <= recent_days <= 7 else None


def _juejin_article_projection(item: object) -> dict[str, object] | None:
    """Project one official search result onto bounded, auditable fields."""
    if not isinstance(item, dict) or item.get("result_type") != 2:
        return None
    result_model = item.get("result_model")
    article_info = (
        result_model.get("article_info") if isinstance(result_model, dict) else None
    )
    if not isinstance(article_info, dict):
        return None
    article_id = str(article_info.get("article_id") or "").strip()
    title = article_info.get("title")
    ctime = article_info.get("ctime")
    if (
        not re.fullmatch(r"\d{8,32}", article_id)
        or not isinstance(title, str)
        or not title.strip()
        or not str(ctime).isdigit()
    ):
        return None
    snippet = article_info.get("brief_content")
    return {
        "article_id": article_id,
        "title": " ".join(title.split())[:240],
        "snippet": (
            " ".join(snippet.split())[:500]
            if isinstance(snippet, str) and snippet.strip()
            else None
        ),
        "published_timestamp": int(str(ctime)),
    }


_JUEJIN_RECRUITMENT_POST_RE = re.compile(
    r"(?:校园招聘|校招|实习生招聘|招聘(?:正式)?启动|招聘岗位|"
    r"内推码|投递简历|欢迎.{0,12}投递|岗位职责|职位要求)",
    re.IGNORECASE,
)


def _juejin_record_matches_goal(record: dict[str, object], task_goal: str) -> bool:
    """Apply only explicit role/cohort hard constraints to one recent post."""
    searchable = " ".join(
        value
        for value in (record.get("title"), record.get("snippet"))
        if isinstance(value, str)
    )
    lowered = searchable.lower()
    goal_lowered = task_goal.lower()
    if _JUEJIN_RECRUITMENT_POST_RE.search(searchable) is None:
        return False
    if "产品经理" in task_goal and "产品经理" not in searchable:
        return False
    if "aigc" in goal_lowered and not any(
        marker in lowered for marker in ("aigc", "生成式", "大模型", "ai 产品")
    ):
        return False
    return not (
        any(marker in task_goal for marker in ("应届", "校招", "毕业生"))
        and not any(
            marker in searchable
            for marker in ("应届", "校招", "校园招聘", "毕业生", "届")
        )
    )


def _search_juejin_recent_posts(
    context: ToolContext,
    payload: SearchPublicJobPagesInput,
    *,
    recent_days: int,
) -> SearchPublicJobPagesOutput:
    """Exhaust Juejin's official recent search and filter exact task bounds."""
    _assert_public_url(_JUEJIN_SEARCH_API_URL)
    task_goal = str(context.metadata.get("task_goal") or "")
    records_by_id: dict[str, dict[str, object]] = {}
    coverage_complete = True
    for keyword in _JUEJIN_RECENT_SEARCH_QUERIES:
        cursor = "0"
        exhausted = False
        for _page_index in range(_JUEJIN_MAX_SEARCH_PAGES_PER_QUERY):
            request_url = _JUEJIN_SEARCH_API_URL + "?" + urlencode(
                {
                    "query": keyword,
                    "id_type": 0,
                    "cursor": cursor,
                    "limit": 20,
                    # Juejin's public UI maps period=2 to 最近一周. The
                    # requested <=7-day bound is applied again below using
                    # each article's official ctime.
                    "search_type": 2,
                    "sort_type": 0,
                    "version": 1,
                    "uuid": str(uuid.uuid4()),
                }
            )
            try:
                fetched = _fetch_validated(request_url)
                response = fetched.response
                response.raise_for_status()
                envelope = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise PublicFetchError("public_search_failed") from exc
            if not isinstance(envelope, dict) or envelope.get("err_no") not in {0, "0"}:
                raise PublicFetchError("public_search_failed")
            raw_records = envelope.get("data")
            if not isinstance(raw_records, list):
                raise PublicFetchError("public_search_failed")
            for raw_record in raw_records:
                record = _juejin_article_projection(raw_record)
                if record is not None:
                    records_by_id[str(record["article_id"])] = record
            has_more = envelope.get("has_more") is True
            next_cursor = str(envelope.get("cursor") or "")
            if not has_more:
                exhausted = True
                break
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        if not exhausted:
            coverage_complete = False

    now_timestamp = int(time.time())
    cutoff_timestamp = now_timestamp - recent_days * 86_400
    window_records = [
        record
        for record in records_by_id.values()
        if cutoff_timestamp <= int(record["published_timestamp"]) <= now_timestamp
    ]
    window_records.sort(
        key=lambda record: (-int(record["published_timestamp"]), str(record["article_id"]))
    )
    matched_records = [
        record
        for record in window_records
        if _juejin_record_matches_goal(record, task_goal)
    ]

    def to_scan_record(record: dict[str, object]) -> PublicCommunityScanRecord:
        timestamp = int(record["published_timestamp"])
        return PublicCommunityScanRecord(
            title=str(record["title"]),
            url=f"https://juejin.cn/post/{record['article_id']}",
            snippet=(
                str(record["snippet"])
                if isinstance(record.get("snippet"), str)
                else None
            ),
            published_at=datetime.fromtimestamp(
                timestamp, tz=UTC
            ).isoformat(),
        )

    scan_evidence = [to_scan_record(record) for record in window_records]
    results = [
        PublicJobSearchResult(
            title=item.title,
            url=item.url,
            snippet=item.snippet,
        )
        for item in scan_evidence
        if item.url
        in {
            f"https://juejin.cn/post/{record['article_id']}"
            for record in matched_records[: payload.max_results]
        }
    ]
    hash_payload = {
        "source": _JUEJIN_SEARCH_API_URL,
        "queries": list(_JUEJIN_RECENT_SEARCH_QUERIES),
        "recent_days": recent_days,
        "coverage_complete": coverage_complete,
        "records": [
            {
                "article_id": record["article_id"],
                "title": record["title"],
                "snippet": record["snippet"],
                "published_timestamp": record["published_timestamp"],
            }
            for record in window_records
        ],
    }
    content_hash = hashlib.sha256(
        json.dumps(
            hash_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return SearchPublicJobPagesOutput(
        query=payload.query,
        source_url=_JUEJIN_SEARCH_API_URL,
        content_hash=content_hash,
        results=results,
        terminal_reason="candidates_found" if results else "search_empty",
        provider="juejin_official_search",
        source_scope="juejin.cn",
        time_window_days=recent_days,
        coverage_complete=coverage_complete,
        scanned_result_count=len(window_records),
        matched_result_count=len(matched_records),
        scan_queries=list(_JUEJIN_RECENT_SEARCH_QUERIES),
        scan_evidence=scan_evidence,
    )


def search_public_job_pages(
    context: ToolContext, payload: SearchPublicJobPagesInput
) -> SearchPublicJobPagesOutput:
    """Search a fixed public provider and return only direct, safe career URLs.

    The query is qualified with recruiting-domain ``site:`` operators unless it
    already steers with one, and results are filtered by the recruiting-host
    whitelist (unknown hosts need a job-shaped URL path, not just JD wording in
    the title), so tutorial/encyclopedia/official-homepage noise (P2/B4) never
    becomes discovery evidence. The 360 fallback runs the raw query: it stays
    the unconstrained escape hatch, with the result filter still applied.
    """
    query = payload.query
    query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
    # The kernel seeds public_search_routes as a step-scoped mutable list
    # (projected by reference into ToolContext.metadata), so repeated queries
    # and over-routing are detected across tool calls.  Fall back to a local
    # list only when invoked outside the application (direct tool tests /
    # recovery path), keeping public_search_query_hashes readable by
    # recovery.py.
    shared_routes = context.metadata.get("public_search_routes")
    if isinstance(shared_routes, list):
        attempted = shared_routes
        context.metadata["public_search_query_hashes"] = shared_routes
    else:
        attempted = context.metadata.setdefault("public_search_query_hashes", [])
        if not isinstance(attempted, list):
            attempted = []
            context.metadata["public_search_query_hashes"] = attempted
    route_limit = (
        _MAX_PUBLIC_SEARCH_ROUTES
        if context.metadata.get("runtime_auto_search") is True
        else _MAX_PUBLIC_SEARCH_ROUTES - 1
    )
    if query_hash in attempted or len(attempted) >= route_limit:
        raise PublicFetchError(
            "route_already_consumed",
            message="本次运行的公开搜索路由已使用完毕，请转入人工确认或使用已有候选页面。",
        )
    attempted.append(query_hash)
    juejin_recent_days = _juejin_recent_days(context)
    if juejin_recent_days is not None:
        return _search_juejin_recent_posts(
            context, payload, recent_days=juejin_recent_days
        )
    if context.metadata.get("runtime_auto_search") is not True:
        # Always qualify the query with a pure OR chain of recruiting-domain
        # site: operators.  _normalize_search_query strips any agent-authored
        # site: fragment and merges it into the campus whitelist so the engine
        # treats every domain as an alternative instead of an implicit AND that
        # starves discovery to empty results.
        query = _normalize_search_query(query)
    search_parameters = {
        "q": query,
        "mkt": "zh-CN",
        "setlang": "zh-hans",
        "cc": "CN",
    }
    source_url = "https://www.bing.com/search?" + urlencode(search_parameters)
    try:
        response = requests.get(
            source_url,
            timeout=20,
            headers={"User-Agent": "CareerAssistantPEV/1.0 (+public-job-search)"},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise PublicFetchError("public_search_failed") from exc
    html = response.text
    parser = _BingSearchResultParser()
    parser.feed(html)
    results: list[PublicJobSearchResult] = []
    seen_urls: set[str] = set()
    blocked_domains = _blocked_domains(context)

    def add_result(raw_result: dict[str, str], result_url: str | None) -> None:
        if result_url is None:
            return
        parsed = urlsplit(result_url)
        if (
            result_url in seen_urls
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.hostname.endswith("bing.com")
            or _domain_scope(result_url) in blocked_domains
            or not _is_plausible_public_job_result(raw_result, result_url, parsed.hostname)
        ):
            return
        try:
            _assert_public_url(result_url)
        except PublicFetchError:
            return
        seen_urls.add(result_url)
        results.append(PublicJobSearchResult(
            title=raw_result["title"],
            url=result_url,
            snippet=raw_result.get("snippet"),
        ))

    for raw_result in parser.results:
        add_result(raw_result, _direct_bing_result_url(raw_result["url"]))
        if len(results) >= payload.max_results:
            break
    if not results:
        fallback_source_url = "https://www.so.com/s?" + urlencode({"q": payload.query})
        try:
            fallback_response = requests.get(
                fallback_source_url,
                timeout=20,
                headers={"User-Agent": "CareerAssistantPEV/1.0 (+public-job-search)"},
            )
            fallback_response.raise_for_status()
        except requests.RequestException as exc:
            raise PublicFetchError("public_search_failed") from exc
        html = fallback_response.text
        source_url = fallback_source_url
        fallback_parser = _SoSearchResultParser()
        fallback_parser.feed(html)
        for raw_result in fallback_parser.results:
            add_result(raw_result, raw_result["url"])
            if len(results) >= payload.max_results:
                break
    if not results:
        # Sogou's mobile HTML exposes the direct public target in the wrapper's
        # ``url`` query parameter and gives materially better Chinese job-query
        # recall than the two desktop providers. We decode only that declared
        # target; ``add_result`` still applies host/path quality filtering and
        # the full public-URL/blocked-domain security checks before returning it.
        mobile_source_url = "https://m.sogou.com/web/searchList.jsp?" + urlencode(
            {"keyword": payload.query}
        )
        try:
            mobile_response = requests.get(
                mobile_source_url,
                timeout=20,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Linux; Android 12; Pixel 5) "
                        "AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"
                    )
                },
            )
            mobile_response.raise_for_status()
        except requests.RequestException:
            pass
        else:
            html = mobile_response.text
            source_url = mobile_source_url
            mobile_parser = _SogouMobileSearchResultParser(mobile_source_url)
            mobile_parser.feed(html)
            for raw_result in _prioritize_direct_search_results(mobile_parser.results):
                add_result(raw_result, raw_result["url"])
                if len(results) >= payload.max_results:
                    break
    return SearchPublicJobPagesOutput(
        query=payload.query,
        source_url=source_url,
        content_hash=hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest(),
        results=results,
        terminal_reason="candidates_found" if results else "search_empty",
    )


def _is_allowed_job_host(hostname: str) -> bool:
    """True when ``hostname`` is a known recruiting domain (P2/B4 whitelist).

    This is a result-quality filter only -- ``_assert_public_url`` remains the
    security gate. Patterns ending in "." match the first label (careers.example,
    career.hebut.edu.cn); plain domains match by suffix (www.liepin.com,
    job.ncss.cn).
    """
    lowered = hostname.lower()
    for pattern in _JOB_SEARCH_ALLOWED_HOST_PATTERNS:
        if pattern.endswith("."):
            if lowered.startswith(pattern):
                return True
        elif lowered == pattern or lowered.endswith("." + pattern):
            return True
    return False


def _is_plausible_public_job_result(
    result: dict[str, str], result_url: str, hostname: str
) -> bool:
    """Keep search evidence useful for job discovery without trusting generic pages.

    Whitelisted recruiting hosts pass on the loose URL-token OR text-signal
    check (their pages are already job-shaped). Any other host must carry a
    job token in its URL path: a tutorial or encyclopedia page rarely does,
    even though its title often mentions 招聘/岗位 -- exactly the noise this
    rejects.
    """
    searchable_text = " ".join(
        value for value in (result.get("title"), result.get("snippet"))
        if isinstance(value, str)
    )
    parsed = urlsplit(result_url)
    allowed_host = _is_allowed_job_host(hostname)
    normalized_path = parsed.path.rstrip("/").lower()
    generic_page = normalized_path.rsplit("/", 1)[-1] in {
        "home",
        "home.html",
        "index",
        "index.html",
    }
    if allowed_host and (normalized_path == "" or generic_page):
        # A recruiting homepage is a source index, not a direct job result.
        # The Executor can still use an explicitly supplied homepage when the
        # user asks for it, but search must not spend fetch budget on it.
        return False
    path_and_query = f"{parsed.path}?{parsed.query}".lower()
    url_token_match = any(
        token in path_and_query
        for token in _JOB_RESULT_URL_TOKENS
        if "." not in token
    )
    if allowed_host:
        return url_token_match or _JOB_RESULT_TEXT_RE.search(searchable_text) is not None
    # Unknown host: a URL job token alone is too weak (a grocery store can
    # publish a /jobs page), but a real recruiting page on an unlisted host
    # (e.g. m.nowcoder.com/job/... or company-a.example/jobs/...) must still
    # pass when its title/snippet or hostname carries a job signal. Reject
    # only the low-signal noise where neither text nor hostname says "job".
    hostname_signal = any(
        token in hostname.lower()
        for token in (
            "career",
            "careers",
            "job",
            "jobs",
            "talent",
            "recruit",
            "recruitment",
            "hr",
            "campus",
            "zhaopin",
            "position",
            "greenhouse",
            "lever",
            "workday",
            "liepin",
            "iguopin",
            "shixiseng",
            "lagou",
            "jobfair",
            "gongzhao",
        )
    )
    return url_token_match and (
        hostname_signal
        or _JOB_RESULT_TEXT_RE.search(searchable_text) is not None
    )


def _direct_bing_result_url(url: str) -> str | None:
    """Decode Bing's documented URL-safe ``u`` redirect value before safety checks."""
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if not hostname.endswith("bing.com"):
        return url
    if not parsed.path.startswith("/ck/"):
        return None
    query = parse_qs(parsed.query)
    encoded = query.get("u", [None])[0]
    if isinstance(encoded, str) and encoded.startswith("a1"):
        encoded = encoded[2:]
    else:
        encoded = query.get("a1", [None])[0]
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        padding = "=" * (-len(encoded) % 4)
        decoded = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None
    decoded_url = urlsplit(decoded)
    if decoded_url.scheme not in {"http", "https"} or not decoded_url.hostname:
        return None
    return decoded


__all__ = [
    "_JOB_SEARCH_ALLOWED_HOST_PATTERNS",
    "_JOB_SEARCH_SITE_OPERATORS",
    "_SITE_OPERATOR_RE",
    "_MAX_PUBLIC_SEARCH_ROUTES",
    "_JUEJIN_SEARCH_API_URL",
    "_JUEJIN_RECENT_SEARCH_QUERIES",
    "_JUEJIN_MAX_SEARCH_PAGES_PER_QUERY",
    "_normalize_search_query",
    "_BingSearchResultParser",
    "_SoSearchResultParser",
    "_SogouMobileSearchResultParser",
    "_juejin_recent_days",
    "_juejin_article_projection",
    "_juejin_record_matches_goal",
    "_search_juejin_recent_posts",
    "search_public_job_pages",
    "_is_allowed_job_host",
    "_is_plausible_public_job_result",
    "_direct_bing_result_url",
]
