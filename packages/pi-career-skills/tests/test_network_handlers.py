"""Hermetic functional tests for the Phase 6 network handlers.

No live network: DNS and the HTTP transport are faked exactly as in
``test_network_security.py`` (per-host ``socket.getaddrinfo`` tables +
``requests.get`` fakes); the Playwright render fallback uses the
``_PLAYWRIGHT_FETCH_IMPL`` seam; the smartsheet bridge uses the
``_list_records_impl`` seam.

Covers the happy paths and routing decisions the security suite must not
clobber: requests fast path, anti-bot domain circuit, render fallback, Bing
direct-result decoding, the per-run search route budget, the classify-url
signal cascade, the §5 validate/dedup re-exports, and the sheet success path.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from pi_career_skills.business.job_discovery import (
    deduplicate_observed,
    validate_candidates,
)
from pi_career_skills.business.job_discovery.handlers import (
    deduplicate_observed_jobs,
    validate_observed_candidates,
)
from pi_career_skills.business.job_discovery.models import (
    ClassifyJobUrlInput,
    FetchPublicJobPageInput,
    QueryCareerSheetRecordsInput,
    SearchPublicJobPagesInput,
)
from pi_career_skills.context import ToolContext
from pi_career_skills.errors import CareerToolError
from pi_career_skills.network import (
    career_sheets,
    classify_url,
    page_fetch,
    playwright_worker,
    public_search,
)

# ---------------------------------------------------------------------------
# Helpers (mirroring test_network_security.py)
# ---------------------------------------------------------------------------


def _context() -> ToolContext:
    return ToolContext(
        user_id="u1",
        run_id="r1",
        attempt_id="a1",
        skill_name="job-discovery",
        metadata={},
    )


def _fake_dns(
    monkeypatch: pytest.MonkeyPatch,
    table: dict[str, list[str]] | None = None,
) -> None:
    import socket

    default_table = {
        "jobs.example.com": ["1.2.3.4"],
        "jobs.example.cn": ["1.2.3.4"],
        "example.com": ["1.2.3.4"],
        "www.bing.com": ["1.2.3.4"],
        "www.so.com": ["1.2.3.4"],
        "m.sogou.com": ["1.2.3.4"],
        "mp.weixin.qq.com": ["1.2.3.4"],
    }
    if table:
        default_table.update(table)

    def fake_getaddrinfo(host: str, port: Any, type: Any = None, **kwargs: Any) -> list[Any]:
        del port, type, kwargs
        addresses = default_table.get(host)
        if not addresses:
            raise OSError(f"name or service not known: {host}")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))
            for address in addresses
        ]

    monkeypatch.setattr(
        "pi_career_skills.network.url_guard.socket.getaddrinfo", fake_getaddrinfo
    )


def _fake_response(
    html: str = "", status: int = 200, location: str | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status,
        headers={"Location": location} if location else {},
        is_redirect=status in {301, 302, 303, 307, 308} and location is not None,
        text=html,
        content=html.encode("utf-8"),
        encoding="utf-8",
        apparent_encoding="utf-8",
        raise_for_status=lambda: (
            None if status < 400 else requests.HTTPError(f"http {status}")
        ),
    )


_JD_HTML = (
    "<html><head><title>算法工程师 - XX公司</title></head><body>"
    "<h1>算法工程师</h1>"
    "<p>岗位职责：负责搜索推荐算法的研发与优化，参与召回、排序与策略迭代，"
    "推动业务指标提升。</p>"
    "<p>任职要求：计算机相关专业硕士及以上学历，具备扎实的机器学习基础，"
    "熟悉 Python，有推荐系统或搜索实践经验者优先，具备良好的团队协作能力，"
    "能独立完成需求分析、方案设计与效果评估。</p>"
    "<p>加分项：有大规模分布式系统经验，参与过 CTR/CVR 预估模型落地，"
    "熟悉 TensorFlow 或 PyTorch 等主流深度学习框架。</p>"
    "<p>工作地点：北京或深圳。</p>"
    "</body></html>"
)


@pytest.fixture(autouse=True)
def _reset_runtime_toggles() -> None:
    playwright_worker._PLAYWRIGHT_FALLBACK_ENABLED = False
    playwright_worker._PLAYWRIGHT_STORAGE_STATE_PATH = None
    playwright_worker._PLAYWRIGHT_FETCH_IMPL = None


# ---------------------------------------------------------------------------
# fetch-public-job-page: requests fast path
# ---------------------------------------------------------------------------


def test_fetch_public_job_page_requests_success_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_dns(monkeypatch)
    monkeypatch.setattr(
        page_fetch.requests, "get", lambda url, **kwargs: _fake_response(_JD_HTML)
    )
    page = page_fetch.fetch_public_job_page(
        _context(), FetchPublicJobPageInput(url="https://jobs.example.com/post/1")
    )
    assert page.source_url == "https://jobs.example.com/post/1"
    assert page.effective_url == "https://jobs.example.com/post/1"
    assert page.http_status == 200
    assert page.title == "算法工程师 - XX公司"
    assert page.quality == "jd_complete"
    assert page.artifact_id == f"observed:{page.content_hash}"
    assert "岗位职责" in page.visible_text


# ---------------------------------------------------------------------------
# fetch-public-job-page: anti-bot + the run-level domain circuit
# ---------------------------------------------------------------------------


def test_anti_bot_blocked_domain_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch)
    monkeypatch.setattr(
        page_fetch.requests,
        "get",
        lambda url, **kwargs: _fake_response("安全验证 请完成人机验证", status=403),
    )
    context = _context()
    with pytest.raises(CareerToolError) as excinfo:
        page_fetch.fetch_public_job_page(
            context, FetchPublicJobPageInput(url="https://jobs.example.com/post/1")
        )
    assert excinfo.value.code == "anti_bot_challenge"
    assert context.metadata["blocked_public_domains"] == ["jobs.example.com"]
    # A second URL on the same domain fails fast without touching the network.
    with pytest.raises(CareerToolError) as excinfo2:
        page_fetch.fetch_public_job_page(
            context, FetchPublicJobPageInput(url="https://jobs.example.com/post/2")
        )
    assert excinfo2.value.code == "domain_temporarily_blocked"


# ---------------------------------------------------------------------------
# fetch-public-job-page: Playwright render fallback (seam)
# ---------------------------------------------------------------------------


def test_playwright_fallback_render_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch)
    monkeypatch.setattr(
        page_fetch.requests,
        "get",
        lambda url, **kwargs: (_ for _ in ()).throw(requests.Timeout("timed out")),
    )
    playwright_worker.enable_playwright_fallback(True)
    playwright_worker._PLAYWRIGHT_FETCH_IMPL = lambda url: (
        "岗位职责：负责客户端开发，参与性能优化。任职要求：三年以上经验。" * 30,
        "客户端工程师",
    )
    page = page_fetch.fetch_public_job_page(
        _context(), FetchPublicJobPageInput(url="https://jobs.example.com/post/1")
    )
    assert page.quality == "jd_complete"
    assert page.title == "客户端工程师"
    assert page.http_status == 200
    assert page.effective_url == "https://jobs.example.com/post/1"


# ---------------------------------------------------------------------------
# search-public-job-pages: Bing direct-result decoding
# ---------------------------------------------------------------------------


def test_search_bing_direct_result_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch)
    target = "https://jobs.example.com/post/12345"
    encoded = base64.urlsafe_b64encode(target.encode("utf-8")).decode("ascii").rstrip("=")
    redirect_url = "https://www.bing.com/ck/a?a1=" + encoded
    bing_html = (
        '<ul><li class="b_algo"><h2>'
        f'<a href="{redirect_url}">XX公司2026届校园招聘启动</a>'
        "</h2><p>岗位职责 算法工程师 工作地点北京</p></li></ul>"
    )

    def fake_get(url: str, **kwargs: Any) -> SimpleNamespace:
        if url.startswith("https://www.bing.com/search?"):
            return _fake_response(bing_html)
        raise AssertionError(f"fallback providers must not be reached: {url}")

    monkeypatch.setattr(public_search.requests, "get", fake_get)
    out = public_search.search_public_job_pages(
        _context(), SearchPublicJobPagesInput(query="算法工程师 校招")
    )
    assert out.terminal_reason == "candidates_found"
    assert out.results[0].url == target
    assert "招聘" in out.results[0].title


# ---------------------------------------------------------------------------
# search-public-job-pages: per-run route budget
# ---------------------------------------------------------------------------


def test_search_route_budget_consumed(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch)
    monkeypatch.setattr(
        public_search.requests, "get", lambda url, **kwargs: _fake_response("<html></html>")
    )
    context = _context()
    public_search.search_public_job_pages(
        context, SearchPublicJobPagesInput(query="算法工程师")
    )
    public_search.search_public_job_pages(
        context, SearchPublicJobPagesInput(query="产品经理")
    )
    with pytest.raises(CareerToolError) as excinfo:
        public_search.search_public_job_pages(
            context, SearchPublicJobPagesInput(query="数据分析师")
        )
    assert excinfo.value.code == "route_already_consumed"
    assert len(context.metadata["public_search_query_hashes"]) == 2


def test_search_route_budget_can_be_boundedly_extended_for_aggregator_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_dns(monkeypatch)
    monkeypatch.setattr(
        public_search.requests, "get", lambda url, **kwargs: _fake_response("<html></html>")
    )
    context = _context()
    context.metadata["search_route_budget"] = 4
    for index in range(4):
        public_search.search_public_job_pages(
            context, SearchPublicJobPagesInput(query=f"AI 岗位 {index}")
        )
    with pytest.raises(CareerToolError) as excinfo:
        public_search.search_public_job_pages(
            context, SearchPublicJobPagesInput(query="AI 岗位 4")
        )
    assert excinfo.value.code == "route_already_consumed"


# ---------------------------------------------------------------------------
# classify-job-url: host -> probe signal cascade
# ---------------------------------------------------------------------------


def test_classify_job_url_cascade(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch)

    def fake_get(url: str, **kwargs: Any) -> SimpleNamespace:
        if "static" in url:
            return _fake_response("<html><body>岗位职责：负责后端开发。任职要求：三年经验。</body></html>")
        return _fake_response(
            "<html><head><title>加载中</title></head><body><script>window.boot();</script></body></html>"
        )

    monkeypatch.setattr(page_fetch.requests, "get", fake_get)
    out = classify_url.classify_job_url(
        _context(),
        ClassifyJobUrlInput(
            urls=[
                "https://mp.weixin.qq.com/s/abc",
                "https://jobs.example.com/static/1",
                "https://jobs.example.com/spa/2",
            ]
        ),
    )
    assert out.results[0].site_class == "wechat"
    assert out.results[0].evidence_signal == "host=mp.weixin.qq.com"
    assert out.results[1].site_class == "static"
    assert out.results[1].evidence_signal == "jd_section_markers"
    assert out.results[2].site_class == "spa"
    assert out.results[2].evidence_signal == "js_bundle_only"


def test_classify_job_url_blocked_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch)

    def fake_get(url: str, **kwargs: Any) -> SimpleNamespace:
        return _fake_response("captcha required", status=429)

    monkeypatch.setattr(page_fetch.requests, "get", fake_get)
    out = classify_url.classify_job_url(
        _context(), ClassifyJobUrlInput(urls=["https://jobs.example.com/blocked/1"])
    )
    assert out.results[0].site_class == "blocked"
    assert out.results[0].evidence_signal == "http_429"


# ---------------------------------------------------------------------------
# §5 move: validate/dedup re-exports keep registry wiring intact
# ---------------------------------------------------------------------------


def test_validate_dedup_reexported_from_handlers() -> None:
    assert validate_observed_candidates is validate_candidates.validate_observed_candidates
    assert deduplicate_observed_jobs is deduplicate_observed.deduplicate_observed_jobs


# ---------------------------------------------------------------------------
# career-sheets: success path with evidence binding
# ---------------------------------------------------------------------------


def test_career_sheet_success_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_list(file_id: str, sheet_id: str, limit: int, offset: int) -> dict[str, Any]:
        del file_id, sheet_id, limit, offset
        return {
            "records": [
                {
                    "field_values": [
                        {"field": "企业名称", "text_value": "腾讯"},
                        {"field": "招聘岗位", "text_value": "算法工程师"},
                        {"field": "投递链接", "text_value": "https://join.qq.com/post/1"},
                        {"field": "更新时间", "text_value": "2026-08-20"},
                    ]
                }
            ],
            "has_more": False,
        }

    monkeypatch.setattr(career_sheets, "_list_records_impl", fake_list)
    out = career_sheets.query_career_sheet_records(
        _context(), QueryCareerSheetRecordsInput(company_keywords=["腾讯"])
    )
    assert out.matched_count == 4, "one record per queried tab"
    assert out.sheets_queried == 4
    record = out.records[0]
    assert record.company_name == "腾讯"
    assert record.apply_url == "https://join.qq.com/post/1"
    assert record.source_url == "https://join.qq.com/post/1"
    assert record.content_hash is not None and len(record.content_hash) == 64
    assert record.prior_metadata is not None
    assert record.prior_metadata.referral_code is None
    assert out.source_url == "https://join.qq.com/post/1"
    assert len(out.content_hash) == 64
