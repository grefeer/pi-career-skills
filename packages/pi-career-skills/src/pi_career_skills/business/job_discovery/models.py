"""Pydantic input/output models for the ten job-discovery skill tools.

Models are ported verbatim from
``skill/job_discovery/runtime/job_discovery.py`` (and sibling runtime
modules). Only the import paths differ — the field definitions,
validators, defaults, and docstrings match the source byte-for-byte so
that JSON-schema comparison with ``docs/pi_contract_snapshot.json``
passes exactly.

Network-side helpers (Playwright, HTTP fetch, etc.) are NOT here — they
belong in :mod:`pi_career_skills.network` (Phase 6).
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Module-private constants for page-quality classification.
# These are deterministic pure-text / pure-URL helpers used by the
# FetchPublicJobPageOutput model_validator.
# ---------------------------------------------------------------------------

_MIN_USABLE_TEXT_CHARS = 160
_MIN_REAL_JD_TEXT_CHARS = 400
_JD_MARKER_SCAN_HEAD_CHARS = 2_000

_JD_SECTION_MARKERS = (
    "岗位职责",
    "岗位要求",
    "职位描述",
    "工作职责",
    "任职要求",
    "职责描述",
    "responsibilities",
)

_URL_COLLECTION_PATH_MARKERS = (
    "/search",
    "/sou/",
    "/topic/",
    "/joblist",
    "/job-list",
    "/zhaopin/",
    "/zhaogongzuo/",
    "/gongsi/",
    "/company/",
    "/simple-login/",
    "/position/",
    "/city-",
    "/list",
)
_URL_SEARCH_QUERY_PARAMS = frozenset({"keyword", "q", "query", "search", "kw"})
_URL_HUB_HOST_PREFIXES = ("careers.", "jobs.", "career.")
_URL_DETAIL_PATH_MARKERS = (
    "/job/detail",
    "/jobs/detail",
    "/jobdetail",
    "/job-detail",
    "/position/detail",
    "/recruitment/position",
    "/zp/",
)


def _detect_js_rendered_list(visible_text: str) -> bool:
    """True when a list page's job rows are client-rendered, not in static text.

    Modern job sites render their job tables (公司\\标题\\地点\\发布时间) with JS:
    the static HTML carries the table header but zero data rows, so the
    captured text reads like a shell and sibling category links point to more
    """
    lines = [line.strip() for line in visible_text.splitlines() if line.strip()]
    if len(lines) < 8:
        return False
    header_line: str | None = None
    for line in lines[:20]:
        if (
            "公司" in line
            and ("职位" in line or "岗位" in line)
            and ("地点" in line or "城市" in line)
        ):
            header_line = line
            break
    if header_line is None:
        return False
    idx = lines.index(header_line)
    row_like = len(re.findall(r"(?:工程师|开发|算法|产品|岗位|职位|招聘|实习|校招)", "\n".join(lines[idx + 1 : idx + 12])))
    return row_like < 6


def _classify_page_quality(
    visible_text: str, *, url: str | None = None
) -> tuple[Literal["jd_complete", "list_only", "js_shell", "empty"], str]:
    """Classify captured text without treating a card shell as a JD.

    This is a routing signal, not a completion gate: the deterministic
    extraction and evidence contracts still decide whether a Skill succeeded.
    """
    normalized = re.sub(r"\s+", "", visible_text or "")
    if not normalized:
        return "empty", "no_visible_text"
    if len(normalized) < _MIN_USABLE_TEXT_CHARS:
        return "js_shell", f"visible_chars<{_MIN_USABLE_TEXT_CHARS}"
    head = normalized[:_JD_MARKER_SCAN_HEAD_CHARS].casefold()
    jd_markers = {
        *(_marker.replace(" ", "").casefold() for _marker in _JD_SECTION_MARKERS),
        "requirements",
        "qualifications",
        "jobresponsibilities",
        "whatyouwilldo",
    }
    if any(marker in head for marker in jd_markers):
        return "jd_complete", "jd_section_marker"
    # Some official career portals render the full JD bodies inline below a
    # long navigation header. The old head-only rule classified those pages as
    # list_only even though the visible evidence already contained repeated
    # responsibilities/requirements sections. Treat that bounded, explicit
    # inline-JD shape as complete; it remains source-backed and extraction
    # still decides which candidate rows are usable.
    inline_markers = (
        "职位描述",
        "工作职责",
        "岗位职责",
        "任职要求",
        "希望你是",
    )
    inline_marker_count = sum(normalized.count(marker) for marker in inline_markers)
    if len(normalized) >= _MIN_REAL_JD_TEXT_CHARS and inline_marker_count >= 2:
        return "jd_complete", "inline_jd_sections"
    if any(
        marker in head
        for marker in ("职位列表", "岗位列表", "招聘职位", "校招职位", "joblist", "jobcards")
    ):
        return "list_only", "list_marker_without_jd_section"
    if _detect_js_rendered_list(visible_text):
        return "list_only", "js_rendered_job_table"
    return "list_only", "usable_text_without_jd_section"


def _url_shape_quality_override(url: str) -> str | None:
    """Return a quality downgrade when *url* unambiguously is a collection page.

    Returns "list_only" for search engines / topic hubs / aggregator zones,
    and None when the URL has no decisive collection shape (detail-shaped or
    generic URLs are left to the text classifier).  This is a *downgrade-only*
    guard: it never upgrades a page to jd_complete.
    """
    if not url:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "/").lower()
    # Detail-shaped URLs are explicitly protected from downgrade.
    if any(marker in path for marker in _URL_DETAIL_PATH_MARKERS):
        return None
    query = parse_qs(parsed.query)
    if any(key in query for key in _URL_SEARCH_QUERY_PARAMS):
        return "list_only"
    if any(marker in path for marker in _URL_COLLECTION_PATH_MARKERS):
        return "list_only"
    # Aggregator result lists (e.g. glassdoor SRCH_...) use a bare search path.
    if "-srch_" in path or "\\sou\\" in path or path.endswith("-jobs"):
        return "list_only"
    # A careers/jobs hub bare root is a navigation hub, not a single JD.
    if path.rstrip("/") in {"", "index", "index.html", "index.htm"} and host.startswith(
        _URL_HUB_HOST_PREFIXES
    ):
        return "list_only"
    return None


def _classify_page_quality_with_url(
    visible_text: str, *, url: str | None = None
) -> tuple[Literal["jd_complete", "list_only", "js_shell", "empty"], str]:
    """Classify page quality with a URL-shape guard applied to jd_complete.

    The text classifier may label a search engine / topic hub / aggregator
    listing as jd_complete when its visible text happens to contain JD markers
    (e.g. a search page whose snippets repeat requirements/qualifications).
    The URL guard is a deterministic, downgrade-only correction: an
    unambiguous collection URL is reduced to list_only no matter what the text
    says, while detail-shaped URLs keep their text classification.
    """
    quality, signal = _classify_page_quality(visible_text)
    if quality == "jd_complete":
        override = _url_shape_quality_override(url or "")
        if override is not None:
            return override, "url_shape_" + override
    return quality, signal


# ---------------------------------------------------------------------------
# fetch-public-job-page / fetch-public-job-pages
# ---------------------------------------------------------------------------


class FetchPublicJobPageInput(BaseModel):
    """One public HTTP(S) URL selected autonomously by Executor."""

    url: str = Field(min_length=1, max_length=2_048)

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("url must not be empty")
        return cleaned


class FetchPublicJobPageOutput(BaseModel):
    artifact_id: str
    source_url: str
    title: str | None
    visible_text: str
    content_hash: str
    effective_url: str | None = None
    redirect_chain: list[str] = Field(default_factory=list)
    http_status: int | None = None
    # Evidence quality is intentionally explicit so downstream Skills can
    # distinguish a usable JD from a list shell without parsing prose.
    quality: Literal["jd_complete", "list_only", "js_shell", "empty"] | None = None
    quality_signal: str | None = None
    # Same-host job-shaped detail URLs captured from this page.  A list_only
    # page exposes the detail routes the agent should fetch next (bounded,
    # deduped, http(s) only), so a list page is a stepping stone, not a dead
    # end.
    detail_links: list[str] = Field(default_factory=list)

    @field_validator("detail_links")
    @classmethod
    def _bound_detail_links(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        kept: list[str] = []
        for link in value:
            if not isinstance(link, str) or not link:
                continue
            link = link.strip()
            if not link.startswith(("http://", "https://")):
                continue
            if link in seen:
                continue
            seen.add(link)
            kept.append(link)
            if len(kept) >= 20:
                break
        return kept

    @model_validator(mode="after")
    def classify_quality(self) -> FetchPublicJobPageOutput:
        if self.quality is not None:
            return self
        # URL-shape guard prefers the post-redirect effective URL, falling back
        # to the requested source_url; both are trusted public URLs by contract.
        url = self.effective_url or self.source_url
        quality, signal = _classify_page_quality_with_url(
            self.visible_text, url=url
        )
        object.__setattr__(self, "quality", quality)
        object.__setattr__(self, "quality_signal", signal)
        return self


class FetchPublicJobPagesInput(BaseModel):
    """A finite Agent-selected set of official public pages to capture at once."""

    urls: list[str] = Field(min_length=1, max_length=10)

    @field_validator("urls")
    @classmethod
    def normalize_urls(cls, values: list[str]) -> list[str]:
        cleaned = [FetchPublicJobPageInput.normalize_url(value) for value in values]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("urls must not contain duplicates")
        return cleaned


class PublicJobPageFetchFailure(BaseModel):
    """One transparent per-page failure; successful evidence remains usable."""

    source_url: str
    error_code: str
    effective_url: str | None = None
    redirect_chain: list[str] = Field(default_factory=list)
    http_status: int | None = None
    message: str | None = None


class FetchPublicJobPagesOutput(BaseModel):
    """All successfully captured pages plus explicit failures from one bounded batch."""

    pages: list[FetchPublicJobPageOutput]
    failures: list[PublicJobPageFetchFailure] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# search-public-job-pages
# ---------------------------------------------------------------------------


class SearchPublicJobPagesInput(BaseModel):
    """A bounded public-web query selected by the Executor from the user's goal."""

    query: str = Field(min_length=2, max_length=400)
    max_results: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("query must not be empty")
        return cleaned


class PublicJobSearchResult(BaseModel):
    """One direct public career link observed in a fixed search-provider response."""

    title: str
    url: str
    snippet: str | None = None


class PublicCommunityScanRecord(PublicJobSearchResult):
    """One timestamped record inspected during an official community scan."""

    published_at: str


class SearchPublicJobPagesOutput(BaseModel):
    """Search evidence that lets the Executor choose a public page to inspect next."""

    query: str
    source_url: str
    content_hash: str
    results: list[PublicJobSearchResult]
    terminal_reason: Literal["candidates_found", "search_empty"] = "candidates_found"
    provider: Literal["public_web_search", "juejin_official_search"] = (
        "public_web_search"
    )
    source_scope: str | None = None
    time_window_days: int | None = Field(default=None, ge=1, le=365)
    coverage_complete: bool = False
    scanned_result_count: int = Field(default=0, ge=0)
    matched_result_count: int = Field(default=0, ge=0)
    scan_queries: list[str] = Field(default_factory=list)
    scan_evidence: list[PublicCommunityScanRecord] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# browse-public-job-page
# ---------------------------------------------------------------------------


class BrowsePublicJobPageInput(BaseModel):
    """A public career URL to open in a headless browser and interact with.

    ``mode`` selects how deep the automation goes (all modes stay within the
    same security envelope: public URLs only, no login, no anti-bot bypass):
      - "render":    stable-text render (same as the fetch fallback path);
      - "load-all":  aggressive scroll + "load more"/"show all" clicks for
                     infinite-scroll / collapsed list pages;
      - "paginate":  render page 1, then jump pages 2..N via the detected
                     URL page pattern (query / offset / path);
      - "interact":  click through up to ``max_cards`` job cards and collect
                     each detail (drawer panels / navigation / go-back).

    Use this tool when ``fetch-public-job-page`` returns a ``js_shell`` or
    ``list_only`` page that needs in-browser interaction to become a full JD.
    """

    url: str = Field(min_length=1, max_length=2_048)
    mode: Literal["render", "load-all", "paginate", "interact"] = "render"
    pages: int = Field(default=3, ge=1, le=5)
    max_cards: int = Field(default=5, ge=1, le=12)
    wait_ms: int = Field(default=1_500, ge=200, le=10_000)

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("url must not be empty")
        return cleaned


class BrowsePublicJobPageOutput(FetchPublicJobPageOutput):
    """Evidence page + the automation steering signals the run-scoped agent
    needs to decide what to do next (how many cards were visible, whether the
    site paginates, which strategy was used, and any warnings)."""

    cards_visible: int | None = None
    estimated_total_items: int | None = None
    pagination_pattern: Literal["query", "offset", "path"] | None = None
    strategy: Literal["render", "load_all", "url_jump", "interact", "single"] | None = None
    strategy_detail: str | None = None
    pages_collected: int | None = None
    warning: str | None = None


# ---------------------------------------------------------------------------
# search-job-site
# ---------------------------------------------------------------------------


class SearchJobSiteInput(BaseModel):
    """Search a career site's own in-site search box for a job keyword.

    Distinct from ``search-public-job-pages`` (external web search): this
    drives the site's native search UI in a headless browser, so results come
    from the site's own index.  Use when a known list page or career portal
    has a search box and the target role needs filtering in place.
    """

    url: str = Field(min_length=1, max_length=2_048)
    query: str = Field(min_length=1, max_length=80)
    max_cards: int = Field(default=5, ge=1, le=12)

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("url must not be empty")
        return cleaned

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("query must not be empty")
        return cleaned


class SearchJobSiteOutput(BaseModel):
    """The post-search page state plus evidence fields, so the result is
    immediately usable as an evidence artifact for extraction.

    ``search_ok=False`` (e.g. no search box found) still returns the rendered
    page text — a truthful, usable fallback rather than a silent failure.
    """

    url: str
    query: str
    effective_url: str | None = None
    search_ok: bool
    search_detail: str | None = None
    pre_search_card_count: int | None = None
    post_search_card_count: int | None = None
    result_indicator: str | None = None
    warning: str | None = None
    # Evidence fields (may be null when the render itself failed downstream).
    artifact_id: str | None = None
    source_url: str | None = None
    content_hash: str | None = None
    visible_text: str = ""
    title: str | None = None
    http_status: int | None = None
    quality: Literal["jd_complete", "list_only", "js_shell", "empty"] | None = None
    quality_signal: str | None = None
    detail_links: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# extract-observed-job-details / batch
# ---------------------------------------------------------------------------


class ExtractObservedJobDetailsInput(BaseModel):
    """The immutable evidence artifact selected by an autonomous Agent."""

    artifact_id: str = Field(min_length=1, max_length=80)


class ExtractedJobDetails(BaseModel):
    """A normalized job DTO whose values remain traceable to one page artifact."""

    title: str | None
    company_name: str | None
    locations: list[str]
    responsibilities: str
    requirements: str
    recruitment_types: list[str]
    apply_url: str | None
    deadline_text: str | None
    published_at: str | None = None
    confidence: float
    evidence_refs: list[dict[str, str]]
    normalization_warnings: list[str]
    # FindJobs-derived structured features (optional v1 fields; see
    # docs/findjobs-optimization-plan.zh-CN.md §6 - no MySQL migration).
    skills: list[str] = Field(default_factory=list)  # A2: closed-set tags
    min_degree: str | None = None                    # B3: degree whitelist value
    priority: str = "unknown"                        # B3: must/preferred/unknown
    # B1: strength dict {score, tier, base_score, evidence[]}; optional.
    strength: dict[str, Any] | None = None
    # B2: taxonomy [level1, level2]; empty list when unclassified.
    taxonomy: list[str] = Field(default_factory=list)


class ExtractObservedJobDetailsOutput(BaseModel):
    """Structured JD candidates derived only from a selected captured page."""

    source_artifact_id: str
    source_url: str
    content_hash: str
    source_quality: Literal["jd_complete", "list_only", "js_shell", "empty"] | None = None
    candidates: list[ExtractedJobDetails]


class ExtractObservedJobDetailsBatchInput(BaseModel):
    """A finite set of previously observed evidence artifacts to normalize."""

    artifact_ids: list[str] = Field(min_length=1, max_length=10)

    @field_validator("artifact_ids")
    @classmethod
    def validate_artifact_ids(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("artifact_ids must be non-empty and unique")
        return cleaned


class ExtractObservedJobDetailsBatchOutput(BaseModel):
    """One structured result per requested immutable public-page artifact."""

    details: list[ExtractObservedJobDetailsOutput]


# ---------------------------------------------------------------------------
# validate-observed-candidates
# ---------------------------------------------------------------------------


class ValidateObservedCandidatesInput(BaseModel):
    """A bounded set of previously observed evidence artifacts to check."""

    artifact_ids: list[str] = Field(min_length=1, max_length=10)

    @field_validator("artifact_ids")
    @classmethod
    def validate_artifact_ids(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("artifact_ids must be non-empty and unique")
        return cleaned


class CandidateIssue(BaseModel):
    """One evidence-quality finding for one artifact."""

    artifact_id: str
    code: str
    detail: str


class ValidateObservedCandidatesOutput(BaseModel):
    """Aggregate quality verdict; valid is False when any issue exists."""

    valid: bool
    issues: list[CandidateIssue]


# ---------------------------------------------------------------------------
# deduplicate-observed-jobs
# ---------------------------------------------------------------------------


class DeduplicateObservedJobsInput(BaseModel):
    """A bounded set of observed evidence artifacts to dedupe in run order."""

    artifact_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("artifact_ids")
    @classmethod
    def validate_artifact_ids(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("artifact_ids must be non-empty and unique")
        return cleaned


class DeduplicatedRemoval(BaseModel):
    """One artifact dropped as duplicate (or unprocessable) by the tool."""

    artifact_id: str
    reason: str
    detail: str


class DeduplicateObservedJobsOutput(BaseModel):
    """First-seen-wins kept list plus explicit removals with reasons."""

    kept: list[str]
    removed: list[DeduplicatedRemoval]


# ---------------------------------------------------------------------------
# classify-job-url
# ---------------------------------------------------------------------------


class ClassifyJobUrlInput(BaseModel):
    """A bounded set of candidate URLs to classify before fetching."""

    urls: list[str] = Field(min_length=1, max_length=10)

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("urls must be non-empty and unique")
        return cleaned


class ClassifiedJobUrl(BaseModel):
    """One URL's site class plus the signal that decided it."""

    url: str
    site_class: str
    evidence_signal: str


class ClassifyJobUrlOutput(BaseModel):
    """Classification results in input order."""

    results: list[ClassifiedJobUrl]


# ---------------------------------------------------------------------------
# query-career-sheet-records
# ---------------------------------------------------------------------------


class QueryCareerSheetRecordsInput(BaseModel):
    """Keywords are substring-matched against company/industry/location/summary."""

    company_keywords: list[str] = Field(default_factory=list, max_length=5)
    role_keywords: list[str] = Field(default_factory=list, max_length=5)
    location_keywords: list[str] = Field(default_factory=list, max_length=5)
    recent_days: int | None = Field(default=None, ge=0, le=365)
    model_config = ConfigDict(extra="forbid")


class CareerSheetPriorMetadata(BaseModel):
    """Smartsheet-carried facts that can fill fields a page lacks.

    Field mapping mirrors smartsheet-sources.md: 企业名称 -> company_name,
    内推/招聘/投递链接 -> apply_url, 内推码(区分大小写) -> referral_code,
    更新时间/更新日期 -> update_time.
    """

    company_name: str | None = None
    apply_url: str | None = None
    referral_code: str | None = None
    update_time: str | None = None


class CareerSheetRecord(BaseModel):
    company_name: str | None = None
    apply_url: str | None = None
    sheet_name: str
    industry: str | None = None
    location: str | None = None
    recruitment_type: str | None = None
    updated_at: str | None = None
    raw_summary: str | None = None
    prior_metadata: CareerSheetPriorMetadata | None = None
    # Evidence binding (C005): the apply URL acts as source_url and the record
    # gets a content hash, so sheet records satisfy the runtime evidence
    # contract and can be persisted as job_search_results artifacts. Both stay
    # None when the record carries no apply URL - nothing to bind to.
    source_url: str | None = None
    content_hash: str | None = None


class QueryCareerSheetRecordsOutput(BaseModel):
    records: list[CareerSheetRecord]
    matched_count: int
    scanned_count: int
    sheets_queried: int
    truncated: bool
    query: dict[str, Any]
    # Output-level evidence binding: source_url is the first bound record's
    # apply URL (the smartsheet file URL when nothing matched) and content_hash
    # covers the whole records payload, so a non-empty query is persistable.
    source_url: str
    content_hash: str


# ---------------------------------------------------------------------------
# fetch-wechat-article
# ---------------------------------------------------------------------------


class FetchWechatArticleInput(BaseModel):
    """One public WeChat article URL (mp.weixin.qq.com or a ReadGZH mirror)."""

    url: str = Field(min_length=1, max_length=2_048)
    out_dir: str | None = None

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("url must not be empty")
        return cleaned


class FetchWechatArticleOutput(BaseModel):
    """OCR slice outcome; a usable text carries the standard page evidence.

    The page-evidence keys (artifact_id / source_url / content_hash /
    visible_text) mirror the fetch contract so a slice that produced text
    enters the observed-evidence pool through ``_with_observed_page``;
    slices without text carry None/"" and are never persisted as evidence.
    """

    url: str
    status: str
    channel: str | None
    candidates: list[ExtractedJobDetails]
    ocr_text: str
    needs_deep_crawl: bool
    reason: str | None
    # Page-evidence keys (same contract as FetchPublicJobPageOutput).
    artifact_id: str | None = None
    source_url: str | None = None
    content_hash: str | None = None
    visible_text: str = ""


__all__ = [
    # fetch
    "FetchPublicJobPageInput",
    "FetchPublicJobPageOutput",
    "FetchPublicJobPagesInput",
    "FetchPublicJobPagesOutput",
    "PublicJobPageFetchFailure",
    # search
    "SearchPublicJobPagesInput",
    "SearchPublicJobPagesOutput",
    "PublicJobSearchResult",
    "PublicCommunityScanRecord",
    # extract
    "ExtractObservedJobDetailsInput",
    "ExtractObservedJobDetailsOutput",
    "ExtractObservedJobDetailsBatchInput",
    "ExtractObservedJobDetailsBatchOutput",
    "ExtractedJobDetails",
    # validate
    "ValidateObservedCandidatesInput",
    "ValidateObservedCandidatesOutput",
    "CandidateIssue",
    # dedup
    "DeduplicateObservedJobsInput",
    "DeduplicateObservedJobsOutput",
    "DeduplicatedRemoval",
    # classify
    "ClassifyJobUrlInput",
    "ClassifyJobUrlOutput",
    "ClassifiedJobUrl",
    # career sheets
    "QueryCareerSheetRecordsInput",
    "QueryCareerSheetRecordsOutput",
    "CareerSheetRecord",
    "CareerSheetPriorMetadata",
    # wechat
    "FetchWechatArticleInput",
    "FetchWechatArticleOutput",
]
