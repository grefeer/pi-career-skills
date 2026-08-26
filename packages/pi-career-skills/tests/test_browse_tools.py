"""Hermetic tests for the P1/P2 browser tools, URL pagination, and the P2 dedup.

No live network: ``playwright_worker._browse_with_playwright`` (the worker
seam) and the requests transport are faked.  The dedup tests drive
``_find_observed_evidence`` through ``metadata["observed_public_evidence"]``
with adapter-record evidence, which is deterministic and needs no extraction.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from pi_career_skills.business.job_discovery.deduplicate_observed import (
    _ArtifactIdentity,
    _evidence_identity,
    _is_echo_of,
    _normalize_company,
    _title_skeleton,
    deduplicate_observed_jobs,
)
from pi_career_skills.business.job_discovery.models import (
    BrowsePublicJobPageInput,
    DeduplicateObservedJobsInput,
    FetchPublicJobPageOutput,
    SearchJobSiteInput,
)
from pi_career_skills.context import ToolContext
from pi_career_skills.errors import CareerToolError
from pi_career_skills.network import batch_fetch, browse, playwright_worker
from pi_career_skills.network.batch_fetch import (
    _merge_detail_links,
    _pagination_sibling_pages,
)
from pi_career_skills.network.url_guard import PublicFetchError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _context(metadata: dict[str, Any] | None = None) -> ToolContext:
    return ToolContext(
        user_id="u1",
        run_id="r1",
        attempt_id="a1",
        skill_name="job-discovery",
        metadata=metadata or {},
    )


def _fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-host DNS table so ``_assert_public_url`` is deterministic."""
    import socket

    table = {
        "jobs.example.com": ["1.2.3.4"],
        "192.168.1.10": ["192.168.1.10"],  # RFC1918 -> unsafe_public_url
    }

    def fake_getaddrinfo(host: str, port: Any, type: Any = None, **kwargs: Any) -> list[Any]:
        del port, type, kwargs
        addresses = table.get(host)
        if not addresses:
            raise OSError(f"name or service not known: {host}")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))
            for address in addresses
        ]

    monkeypatch.setattr(
        "pi_career_skills.network.url_guard.socket.getaddrinfo", fake_getaddrinfo
    )


def _jd_body(seed: str = "") -> str:
    """A clean >=160-char JD body (no access-block / dead-link markers)."""
    return (
        f"{seed}岗位职责：负责搜索推荐算法的研发与优化，参与召回、排序与策略迭代，"
        "推动业务指标提升，与产品、运营团队紧密协作完成需求分析、方案设计与效果评估，"
        "并持续跟踪线上指标波动，定位策略问题并给出优化方案。"
        "任职要求：计算机相关专业本科及以上学历，具备扎实的机器学习基础，熟悉 Python，"
        "有推荐系统或搜索实践经验者优先，具备良好的团队协作能力，能独立完成任务，"
        "对新技术保持好奇心并愿意主动学习。"
    )


def _list_body(page_label: str) -> str:
    """A clean list-page body (no JD section markers in the head)."""
    return (
        f"职位列表 - {page_label}\n"
        "算法工程师｜某科技公司｜北京\n"
        "后端开发工程师｜某互联网公司｜上海\n"
        "产品经理｜某电商公司｜深圳\n"
        "前端开发工程师｜某教育公司｜杭州\n"
    ) * 4


def _browse_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "body": _jd_body(),
        "title": "算法工程师 - 测试公司",
        "effective_url": "https://jobs.example.com/positions?page=1",
        "status_code": 200,
        "links": ["https://jobs.example.com/post/1"],
        "cards_visible": 12,
        "estimated_total_items": 60,
        "pagination_pattern": "query",
        "strategy": "url_jump",
        "strategy_detail": "jumped to page 2",
        "pages_collected": 2,
        "warning": None,
    }
    payload.update(overrides)
    return payload


def _record_evidence(
    artifact_id: str,
    records: list[dict[str, Any]],
    *,
    content_hash: str | None = None,
    quality: str | None = "jd_complete",
) -> dict[str, Any]:
    """Observed-public-evidence entry whose body is adapter JSON records.

    content_hash defaults to a value unique per artifact_id, so the P2 ``hash:``
    identity key only ever collides when a test opts in by passing the same
    hash explicitly (see ``test_dedup_content_hash_identity``).
    """
    return {
        "artifact_id": artifact_id,
        "source_url": f"https://jobs.example.com/{artifact_id}",
        "content_hash": content_hash
        or hashlib.sha256(artifact_id.encode("utf-8")).hexdigest(),
        "visible_text": json.dumps(records, ensure_ascii=False),
        "quality": quality,
    }


# ---------------------------------------------------------------------------
# browse-public-job-page
# ---------------------------------------------------------------------------


def test_browse_public_job_page_passes_mode_and_maps_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_dns(monkeypatch)
    calls: dict[str, Any] = {}

    def fake_browse(
        url: str, *, mode: str, pages: int, max_cards: int, wait_ms: int, **_: Any
    ) -> dict[str, Any]:
        calls.update(url=url, mode=mode, pages=pages, max_cards=max_cards, wait_ms=wait_ms)
        return _browse_payload()

    monkeypatch.setattr(playwright_worker, "_browse_with_playwright", fake_browse)
    out = browse.browse_public_job_page(
        _context(),
        BrowsePublicJobPageInput(url="https://jobs.example.com/positions"),
    )
    assert calls["url"] == "https://jobs.example.com/positions"
    assert calls["mode"] == "render"
    assert calls["pages"] == 3
    assert calls["max_cards"] == 5
    assert calls["wait_ms"] == 1500
    # Evidence contract (same as fetch-public-job-page)
    assert out.quality == "jd_complete"
    assert len(out.content_hash) == 64
    assert out.source_url == "https://jobs.example.com/positions"
    assert out.effective_url == "https://jobs.example.com/positions?page=1"
    assert out.detail_links == ["https://jobs.example.com/post/1"]
    # Steering signals
    assert out.cards_visible == 12
    assert out.estimated_total_items == 60
    assert out.pagination_pattern == "query"
    assert out.strategy == "url_jump"
    assert out.pages_collected == 2
    assert out.warning is None


def test_browse_public_job_page_paginate_mode_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_dns(monkeypatch)
    calls: dict[str, Any] = {}

    def fake_browse(
        url: str, *, mode: str, pages: int, max_cards: int, wait_ms: int, **_: Any
    ) -> dict[str, Any]:
        calls.update(mode=mode, pages=pages, max_cards=max_cards, wait_ms=wait_ms)
        return _browse_payload(strategy="url_jump", pages_collected=3)

    monkeypatch.setattr(playwright_worker, "_browse_with_playwright", fake_browse)
    out = browse.browse_public_job_page(
        _context(),
        BrowsePublicJobPageInput(
            url="https://jobs.example.com/positions?page=1",
            mode="paginate",
            pages=5,
            max_cards=2,
            wait_ms=800,
        ),
    )
    assert calls["mode"] == "paginate"
    assert calls["pages"] == 5
    assert calls["max_cards"] == 2
    assert calls["wait_ms"] == 800
    assert out.pages_collected == 3


def test_browse_public_job_page_blocked_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_dns(monkeypatch)

    def fake_browse(*_: Any, **__: Any) -> dict[str, Any]:
        raise PublicFetchError(
            "anti_bot_challenge",
            message="需要验证码",
            effective_url="https://jobs.example.com/positions",
            status_code=429,
        )

    monkeypatch.setattr(playwright_worker, "_browse_with_playwright", fake_browse)
    context = _context()
    with pytest.raises(PublicFetchError):
        browse.browse_public_job_page(
            context,
            BrowsePublicJobPageInput(url="https://jobs.example.com/positions"),
        )
    assert "jobs.example.com" in context.metadata["blocked_public_domains"]


def test_browse_public_job_page_rejects_private_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch)
    with pytest.raises(CareerToolError):
        browse.browse_public_job_page(
            _context(),
            BrowsePublicJobPageInput(url="http://192.168.1.10/jobs"),
        )


# ---------------------------------------------------------------------------
# search-job-site
# ---------------------------------------------------------------------------


def test_search_job_site_maps_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch)
    calls: dict[str, Any] = {}

    def fake_search(
        url: str, *, mode: str, max_cards: int, wait_ms: int, term: str, **_: Any
    ) -> dict[str, Any]:
        calls.update(url=url, mode=mode, max_cards=max_cards, wait_ms=wait_ms, term=term)
        return _browse_payload(
            body=_jd_body("搜索结果"),
            search_ok=True,
            search_detail="found input and clicked search",
            pre_search_card_count=40,
            post_search_card_count=3,
            result_indicator="共 3 个职位",
            warning=None,
        )

    monkeypatch.setattr(playwright_worker, "_browse_with_playwright", fake_search)
    out = browse.search_job_site(
        _context(),
        SearchJobSiteInput(url="https://jobs.example.com/positions", query="算法"),
    )
    assert calls["mode"] == "search"
    assert calls["term"] == "算法"
    assert calls["wait_ms"] == 1500
    assert out.query == "算法"
    assert out.search_ok is True
    assert out.pre_search_card_count == 40
    assert out.post_search_card_count == 3
    assert out.result_indicator == "共 3 个职位"
    assert out.quality == "jd_complete"
    assert len(out.content_hash) == 64


def test_search_job_site_warns_on_fake_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_dns(monkeypatch)
    def fake_search(*_: Any, **__: Any) -> dict[str, Any]:
        return _browse_payload(
            body=_jd_body("搜索页"),
            search_ok=True,
            pre_search_card_count=30,
            post_search_card_count=30,
            warning="post-search card count equals pre-search count (fake filter)",
        )

    monkeypatch.setattr(playwright_worker, "_browse_with_playwright", fake_search)
    out = browse.search_job_site(
        _context(),
        SearchJobSiteInput(url="https://jobs.example.com/positions", query="后端"),
    )
    assert out.post_search_card_count == 30
    assert "fake filter" in (out.warning or "")


# ---------------------------------------------------------------------------
# batch_fetch: URL pagination (P1-1)
# ---------------------------------------------------------------------------


def test_merge_detail_links_unions_preserving_order() -> None:
    merged = _merge_detail_links(
        ["/a", "/b", "/a"], ["/c", "/b"], []
    )
    assert merged == ["/a", "/b", "/c"]


def test_pagination_sibling_pages_collects_pages_and_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_dns(monkeypatch)  # collector resolves hrefs through _is_public_url
    pattern = {"kind": "query", "key": "page"}
    monkeypatch.setattr(
        playwright_worker,
        "_detect_url_page_pattern",
        lambda url: pattern,
    )
    monkeypatch.setattr(
        playwright_worker,
        "_build_page_url",
        lambda base, _pat, page: f"https://jobs.example.com/positions?page={page}",
    )

    fetched: dict[int, tuple[FetchPublicJobPageOutput, str]] = {
        2: (
            _page_evidence(
                "https://jobs.example.com/positions?page=2", _list_body("第2页")
            ),
            "<html>page2 with <a href='/jobs/20'>p20</a></html>",
        ),
        3: (
            _page_evidence(
                "https://jobs.example.com/positions?page=3", _list_body("第3页")
            ),
            "<html>page3 with <a href='/jobs/30'>p30</a></html>",
        ),
    }

    def fake_fetch(url: str) -> tuple[FetchPublicJobPageOutput, str]:
        page_no = int(url.split("page=")[1])
        if page_no not in fetched:
            raise PublicFetchError("http_error:404", message="page 4 missing")
        return fetched[page_no]

    monkeypatch.setattr(batch_fetch, "_fetch_public_page_requests_with_html", fake_fetch)
    siblings, extra_links = _pagination_sibling_pages(
        "https://jobs.example.com/positions?page=1",
        _list_body("第1页"),
    )
    assert [page.source_url for page in siblings] == [
        "https://jobs.example.com/positions?page=2",
        "https://jobs.example.com/positions?page=3",
    ]
    # Collector resolves relative hrefs against the page URL and filters to
    # job-shaped paths.
    assert extra_links == [
        "https://jobs.example.com/jobs/20",
        "https://jobs.example.com/jobs/30",
    ]


def test_pagination_sibling_pages_stops_on_jd_detail_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pattern = {"kind": "query", "key": "page"}
    monkeypatch.setattr(playwright_worker, "_detect_url_page_pattern", lambda url: pattern)
    siblings, extra_links = _pagination_sibling_pages(
        "https://jobs.example.com/positions?page=1",
        _jd_body(),  # a JD detail page must never be paginated
    )
    assert siblings == []
    assert extra_links == []


def test_pagination_sibling_pages_requires_url_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(playwright_worker, "_detect_url_page_pattern", lambda url: None)
    siblings, extra_links = _pagination_sibling_pages(
        "https://jobs.example.com/positions",
        _list_body("第1页"),
    )
    assert siblings == []
    assert extra_links == []


def _page_evidence(url: str, body: str) -> FetchPublicJobPageOutput:
    from pi_career_skills.network.page_fetch import _build_evidence_page

    return _build_evidence_page(
        requested_url=url,
        effective_url=url,
        title="职位列表",
        visible_text=body,
        status_code=200,
    )


# ---------------------------------------------------------------------------
# deduplicate-observed-jobs P2 upgrade
# ---------------------------------------------------------------------------


def test_dedup_content_hash_identity() -> None:
    """Two artifacts with the same normalized-text hash are the same page."""
    evidence = [
        _record_evidence(
            "a1", [{"title": "算法工程师", "description": "A", "apply_url": "/x"}],
            content_hash="h" * 64,
        ),
        _record_evidence(
            "a2", [{"title": "后端工程师", "description": "B", "apply_url": "/y"}],
            content_hash="h" * 64,  # same hash, different records
        ),
    ]
    context = _context({"observed_public_evidence": evidence})
    out = deduplicate_observed_jobs(
        context,
        DeduplicateObservedJobsInput(artifact_ids=["a1", "a2"]),
    )
    assert out.kept == ["a1"]
    assert out.removed[0].reason == "duplicate_identity"


def test_dedup_company_scoping_keeps_same_title_different_company() -> None:
    """Same-named roles at different companies must not merge."""
    evidence = [
        _record_evidence(
            "a1",
            [
                {
                    "title": "算法工程师",
                    "company": "腾讯",
                    "location": "北京",
                    "description": "JD A",
                    "apply_url": "/tencent/1",
                }
            ],
        ),
        _record_evidence(
            "a2",
            [
                {
                    "title": "算法工程师",
                    "company": "字节跳动",
                    "location": "北京",
                    "description": "JD B",
                    "apply_url": "/bytedance/1",
                }
            ],
        ),
    ]
    context = _context({"observed_public_evidence": evidence})
    out = deduplicate_observed_jobs(
        context,
        DeduplicateObservedJobsInput(artifact_ids=["a1", "a2"]),
    )
    assert out.kept == ["a1", "a2"]
    assert out.removed == []


def test_dedup_same_company_same_title_duplicate() -> None:
    evidence = [
        _record_evidence(
            "a1",
            [{"title": "算法工程师", "company": "腾讯", "description": "JD A", "apply_url": "/1"}],
        ),
        _record_evidence(
            "a2",
            [{"title": "算法工程师", "company": "腾讯", "description": "JD B", "apply_url": "/2"}],
        ),
    ]
    context = _context({"observed_public_evidence": evidence})
    out = deduplicate_observed_jobs(
        context,
        DeduplicateObservedJobsInput(artifact_ids=["a1", "a2"]),
    )
    assert out.kept == ["a1"]
    assert out.removed[0].reason == "duplicate_identity"


def _short_record_evidence(artifact_id: str, title: str, company: str) -> dict[str, Any]:
    return _record_evidence(
        artifact_id,
        [
            {
                "title": title,
                "company": company,
                "description": "这是一条搜索结果摘要。",  # snippet-scale
                "apply_url": f"/{artifact_id}",
            }
        ],
        quality="list_only",
    )


def test_dedup_echo_drop_full_jd_after_snippet() -> None:
    """Snippet first, full JD second: the richer artifact wins (order-independent)."""
    snippet = _short_record_evidence("a1", "Java开发工程师", "腾讯")
    full = _record_evidence(
        "a2",
        [
            {
                "title": "Java开发工程师",
                "company": "腾讯",
                "description": _jd_body("完整JD。") * 6,  # ~2000 chars
                "apply_url": "/a2",
            }
        ],
        quality="jd_complete",
    )
    context = _context({"observed_public_evidence": [snippet, full]})
    out = deduplicate_observed_jobs(
        context,
        DeduplicateObservedJobsInput(artifact_ids=["a1", "a2"]),
    )
    assert out.kept == ["a2"], "full JD must replace the earlier snippet"
    assert len(out.removed) == 1
    assert out.removed[0].artifact_id == "a1"
    assert out.removed[0].reason == "echo_of"


def test_dedup_echo_drop_snippet_after_full_jd() -> None:
    """Full JD first, snippet second: the snippet is dropped."""
    snippet = _short_record_evidence("a2", "Java开发工程师", "腾讯")
    full = _record_evidence(
        "a1",
        [
            {
                "title": "Java开发工程师",
                "company": "腾讯",
                "description": _jd_body("完整JD。") * 6,
                "apply_url": "/a1",
            }
        ],
        quality="jd_complete",
    )
    context = _context({"observed_public_evidence": [full, snippet]})
    out = deduplicate_observed_jobs(
        context,
        DeduplicateObservedJobsInput(artifact_ids=["a1", "a2"]),
    )
    assert out.kept == ["a1"]
    assert out.removed[0].artifact_id == "a2"
    assert out.removed[0].reason == "echo_of"


def test_dedup_cluster_only_match_keeps_both_when_no_echo() -> None:
    """A skeleton-only match between two real pages is not a duplicate."""
    evidence = [
        _record_evidence(
            "a1",
            [
                {
                    "title": "Java(初级)开发工程师",
                    "company": "腾讯",
                    "description": _jd_body("JD A。") * 6,
                    "apply_url": "/a1",
                }
            ],
            quality="jd_complete",
        ),
        _record_evidence(
            "a2",
            [
                {
                    "title": "Java开发工程师",
                    "company": "腾讯",
                    "description": _jd_body("JD B。") * 6,
                    "apply_url": "/a2",
                }
            ],
            quality="jd_complete",
        ),
    ]
    context = _context({"observed_public_evidence": evidence})
    out = deduplicate_observed_jobs(
        context,
        DeduplicateObservedJobsInput(artifact_ids=["a1", "a2"]),
    )
    assert out.kept == ["a1", "a2"]
    assert out.removed == []


def test_dedup_echo_respects_company_mismatch() -> None:
    """A snippet of one company must not be folded into a JD of another."""
    snippet = _short_record_evidence("a1", "Java开发工程师", "字节跳动")
    full = _record_evidence(
        "a2",
        [
            {
                "title": "Java开发工程师",
                "company": "腾讯",
                "description": _jd_body("完整JD。") * 6,
                "apply_url": "/a2",
            }
        ],
        quality="jd_complete",
    )
    context = _context({"observed_public_evidence": [snippet, full]})
    out = deduplicate_observed_jobs(
        context,
        DeduplicateObservedJobsInput(artifact_ids=["a1", "a2"]),
    )
    # Titles differ only by company scoping, so no hard collision at all —
    # both survive.  (The cluster key is weak and company-incompatible echoes
    # are never dropped.)
    assert out.kept == ["a1", "a2"]


def test_title_skeleton_and_company_normalization() -> None:
    assert _title_skeleton("Java开发工程师（急聘）") == "java开发工程师"
    assert _title_skeleton("Java 开发工程师") == "java开发工程师"
    assert _title_skeleton("Java(初级)开发工程师") == "java开发工程师"
    assert _title_skeleton(None) == ""
    assert _normalize_company("  腾讯科技（深圳）有限公司 ") == "腾讯科技深圳有限公司"
    assert _normalize_company(None) is None


def test_is_echo_of_gates() -> None:
    snippet = _ArtifactIdentity((), "title-skel:x", "腾讯", 200, "list_only")
    keeper = _ArtifactIdentity((), "title-skel:x", "腾讯", 3000, "jd_complete")
    assert _is_echo_of(snippet, keeper) is True
    # keeper too thin
    thin = _ArtifactIdentity((), "title-skel:x", "腾讯", 300, "jd_complete")
    assert _is_echo_of(snippet, thin) is False
    # snippet is itself a complete JD
    full_snippet = _ArtifactIdentity((), "title-skel:x", "腾讯", 200, "jd_complete")
    assert _is_echo_of(full_snippet, keeper) is False
    # company mismatch
    other = _ArtifactIdentity((), "title-skel:x", "字节跳动", 200, "list_only")
    assert _is_echo_of(other, keeper) is False


def test_evidence_identity_hash_key_survives_unparseable_text() -> None:
    evidence = {
        "artifact_id": "x1",
        "source_url": "https://jobs.example.com/x1",
        "content_hash": "a" * 64,
        "visible_text": "some plain text with no JD structure at all",
        "quality": "js_shell",
    }
    identity = _evidence_identity(_context(), "x1", evidence)
    assert "hash:" + "a" * 64 in identity.keys
