"""Hermetic security tests for the Phase 6 network layer.

No live network anywhere: DNS is faked by monkeypatching
``socket.getaddrinfo`` (per-host IP tables, global/private/unresolvable), the
HTTP transport is faked by monkeypatching ``requests.get``, the Playwright
render subprocess is faked by monkeypatching ``subprocess.Popen``, and the
smartsheet bridge is faked via the ``_list_records_impl`` seam.

Task H required coverage: redirect-to-private rejected; DNS-private rejected;
unresolvable host; timeout; oversize response; batch partial-failure
isolation; Playwright cleanup/terminate path; OCR disabled ->
``wechat_ocr_disabled``; sheet fallback (sheet fails -> stable error code, no
crash).
"""

from __future__ import annotations

import json
import socket
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from pi_career_skills.business.job_discovery.models import (
    FetchPublicJobPageInput,
    FetchPublicJobPagesInput,
    FetchWechatArticleInput,
    QueryCareerSheetRecordsInput,
)
from pi_career_skills.context import ToolContext
from pi_career_skills.errors import CareerToolError
from pi_career_skills.network import (
    batch_fetch,
    career_sheets,
    page_fetch,
    playwright_worker,
    url_guard,
    wechat,
)

# ---------------------------------------------------------------------------
# Helpers
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
    """Replace ``socket.getaddrinfo`` with a per-host IP table.

    Hosts absent from the table raise ``OSError`` (unresolvable). The default
    table resolves every host used by the hermetic fixtures to a global IP so
    the tests exercise redirect/DNS logic rather than the harness.
    """
    default_table = {
        "jobs.example.com": ["1.2.3.4"],
        "example.com": ["1.2.3.4"],
        "www.bing.com": ["1.2.3.4"],
        "www.so.com": ["1.2.3.4"],
        "m.sogou.com": ["1.2.3.4"],
        "api.juejin.cn": ["1.2.3.4"],
        "gp-api.iguopin.com": ["1.2.3.4"],
        "careers.tencent.com": ["1.2.3.4"],
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
    """A requests-like response fake with the attributes the fetchers read."""
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


@pytest.fixture(autouse=True)
def _reset_runtime_toggles() -> None:
    """Restore the module-level runtime gates after every test."""
    playwright_worker._PLAYWRIGHT_FALLBACK_ENABLED = False
    playwright_worker._PLAYWRIGHT_STORAGE_STATE_PATH = None
    playwright_worker._PLAYWRIGHT_FETCH_IMPL = None
    wechat._WECHAT_OCR_ENABLED = False


# ---------------------------------------------------------------------------
# Public-URL guard: DNS-level rejections
# ---------------------------------------------------------------------------


def test_assert_public_url_rejects_userinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch)
    with pytest.raises(CareerToolError) as excinfo:
        url_guard._assert_public_url("http://user:pass@jobs.example.com/")
    assert excinfo.value.code == "unsafe_public_url"


def test_assert_public_url_rejects_private_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch, table={"jobs.example.com": ["127.0.0.1"]})
    with pytest.raises(CareerToolError) as excinfo:
        url_guard._assert_public_url("http://jobs.example.com/")
    assert excinfo.value.code == "unsafe_public_url"


def test_assert_public_url_unresolvable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch)
    with pytest.raises(CareerToolError) as excinfo:
        url_guard._assert_public_url("http://no-such-host.invalid/")
    assert excinfo.value.code == "public_host_unresolvable"


# ---------------------------------------------------------------------------
# Manual redirect walk: every hop re-validated
# ---------------------------------------------------------------------------


def test_redirect_to_private_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch, table={"127.0.0.1": ["127.0.0.1"]})
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> SimpleNamespace:
        calls.append(url)
        assert url == "https://jobs.example.com/post/1", "walk must stop before the hop"
        return _fake_response("", status=302, location="http://127.0.0.1/secret")

    monkeypatch.setattr(page_fetch.requests, "get", fake_get)
    with pytest.raises(CareerToolError) as excinfo:
        page_fetch._fetch_validated("https://jobs.example.com/post/1")
    assert excinfo.value.code == "unsafe_public_url"
    assert excinfo.value.effective_url == "http://127.0.0.1/secret"
    assert excinfo.value.redirect_chain[-1] == "http://127.0.0.1/secret"
    assert len(calls) == 1


def test_redirect_to_cloud_metadata_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch, table={"169.254.169.254": ["169.254.169.254"]})
    monkeypatch.setattr(
        page_fetch.requests,
        "get",
        lambda url, **kwargs: _fake_response(
            "", status=302, location="http://169.254.169.254/latest/meta-data/"
        ),
    )
    with pytest.raises(CareerToolError) as excinfo:
        page_fetch._fetch_validated("https://jobs.example.com/post/1")
    assert excinfo.value.code == "unsafe_public_url"


# ---------------------------------------------------------------------------
# Transport timeouts
# ---------------------------------------------------------------------------


def test_fetch_validated_timeout_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch)
    calls = {"count": 0}

    def fake_get(url: str, **kwargs: Any) -> SimpleNamespace:
        del url, kwargs
        calls["count"] += 1
        raise requests.Timeout("timed out")

    monkeypatch.setattr(page_fetch.requests, "get", fake_get)
    with pytest.raises(requests.Timeout):
        page_fetch._fetch_validated("https://jobs.example.com/post/1")
    assert calls["count"] == 2, "one transport retry, then the failure stays final"


def test_fetch_public_job_page_timeout_stable_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_dns(monkeypatch)
    monkeypatch.setattr(
        page_fetch.requests,
        "get",
        lambda url, **kwargs: (_ for _ in ()).throw(requests.Timeout("timed out")),
    )
    with pytest.raises(CareerToolError) as excinfo:
        page_fetch.fetch_public_job_page(
            _context(), FetchPublicJobPageInput(url="https://jobs.example.com/post/1")
        )
    # Playwright fallback is off by default -> the stable code surfaces.
    assert excinfo.value.code == "public_fetch_failed"


# ---------------------------------------------------------------------------
# Oversize responses: bounded evidence
# ---------------------------------------------------------------------------


def test_oversize_page_visible_text_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    _fake_dns(monkeypatch)
    body = "岗位职责：负责搜索推荐算法，参与召回与排序优化。" * 4000
    monkeypatch.setattr(
        page_fetch.requests,
        "get",
        lambda url, **kwargs: _fake_response(
            f"<html><head><title>算法工程师</title></head><body>{body}</body></html>"
        ),
    )
    page = page_fetch.fetch_public_job_page(
        _context(), FetchPublicJobPageInput(url="https://jobs.example.com/post/1")
    )
    normalized = page_fetch._normalize_visible_text(body)
    assert page.visible_text == normalized[: page_fetch._MAX_VISIBLE_TEXT_CHARS]
    assert len(page.visible_text) == page_fetch._MAX_VISIBLE_TEXT_CHARS
    assert page.content_hash == hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Batch partial-failure isolation
# ---------------------------------------------------------------------------


def test_batch_partial_failure_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch)
    ok_url = "https://jobs.example.com/post/1"
    bad_url = "https://jobs.example.com/post/2"
    ok_body = (
        "<html><body>岗位职责：负责后端服务的开发与维护，参与系统架构设计与核心模块实现，"
        "保障线上服务稳定性。任职要求：计算机相关专业本科及以上学历，三年以上后端开发经验，"
        "熟悉 Python/MySQL，具备良好的沟通与协作能力，能独立完成需求分析、技术方案设计与"
        "代码评审。工作地点：北京或深圳。</body></html>"
    )
    # Keep the fixture above the source's 160-character evidence floor: this
    # test isolates batch failure handling rather than short-page rejection.
    ok_html = ok_body + ok_body

    def fake_get(url: str, **kwargs: Any) -> SimpleNamespace:
        if url == bad_url:
            raise requests.Timeout("boom")
        return _fake_response(ok_html)

    monkeypatch.setattr(page_fetch.requests, "get", fake_get)
    context = _context()
    out = batch_fetch.fetch_public_job_pages(
        context, FetchPublicJobPagesInput(urls=[ok_url, bad_url])
    )
    # The successful page survives; the failing URL is an explicit failure.
    assert len(out.pages) == 1
    assert out.pages[0].source_url == ok_url
    assert out.pages[0].quality == "jd_complete"
    assert len(out.failures) == 1
    assert out.failures[0].source_url == bad_url
    assert out.failures[0].error_code == "public_fetch_failed"


# ---------------------------------------------------------------------------
# Playwright subprocess: terminate/cleanup path
# ---------------------------------------------------------------------------


def test_playwright_process_timeout_terminates_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_dns(monkeypatch)
    terminated: list[int] = []
    monkeypatch.setattr(
        playwright_worker, "_terminate_process_tree", lambda pid: terminated.append(pid)
    )
    calls = {"count": 0}

    class _FakeProcess:
        pid = 4242
        returncode = 0

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            calls["count"] += 1
            if calls["count"] == 1:
                raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
            return "{}", ""

        def kill(self) -> None:
            pass

    monkeypatch.setattr(
        playwright_worker.subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeProcess(),
    )
    with pytest.raises(CareerToolError) as excinfo:
        playwright_worker._render_with_playwright_process(
            "https://jobs.example.com/post/1", collect_links=False
        )
    assert excinfo.value.code == "public_fetch_failed"
    assert terminated == [4242], "the child process tree is terminated on timeout"


def test_playwright_process_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch)
    payload = json.dumps(
        {
            "body": "岗位职责：负责前端开发。",
            "title": "前端工程师",
            "effective_url": "https://jobs.example.com/post/1",
            "status_code": 200,
        }
    )

    class _FakeProcess:
        pid = 7
        returncode = 0

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            return payload, ""

        def kill(self) -> None:
            pass

    monkeypatch.setattr(
        playwright_worker.subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeProcess(),
    )
    body, title = playwright_worker._render_with_playwright_process(
        "https://jobs.example.com/post/1", collect_links=False
    )
    assert title == "前端工程师"
    assert "岗位职责" in body
    assert playwright_worker._render_metadata("https://jobs.example.com/post/1") == (
        "https://jobs.example.com/post/1",
        200,
    )


def test_playwright_never_uses_a_login_storage_state() -> None:
    """Phase 6 is public-web only: a profile path must not reach Chromium."""
    playwright_worker.configure_playwright_storage_state("C:/profiles/logged-in.json")
    command = playwright_worker._playwright_worker_command(
        "https://jobs.example.com/post/1", collect_links=False
    )
    assert "--storage-state" not in command


# ---------------------------------------------------------------------------
# WeChat OCR channel: gated off -> stable code, no network
# ---------------------------------------------------------------------------


def test_wechat_ocr_disabled_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dns(monkeypatch)

    def no_network(url: str, **kwargs: Any) -> SimpleNamespace:
        del url, kwargs
        raise AssertionError("no network may be touched when the OCR channel is off")

    monkeypatch.setattr(page_fetch.requests, "get", no_network)
    context = _context()
    out = wechat.fetch_wechat_article(
        context, FetchWechatArticleInput(url="https://mp.weixin.qq.com/s/abc123")
    )
    assert out.status == "needs_manual_review"
    assert out.reason == "ocr_disabled"
    # The full tool path surfaces the stable code for a WeChat URL.
    with pytest.raises(CareerToolError) as excinfo:
        page_fetch.fetch_public_job_page(
            context, FetchPublicJobPageInput(url="https://mp.weixin.qq.com/s/abc123")
        )
    assert excinfo.value.code == "wechat_ocr_disabled"


# ---------------------------------------------------------------------------
# Smartsheet bridge: stable failure codes, no crash
# ---------------------------------------------------------------------------


def test_sheet_rate_limited_yields_stable_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(file_id: str, sheet_id: str, limit: int, offset: int) -> dict[str, Any]:
        del file_id, sheet_id, limit, offset
        raise career_sheets.SheetQueryError("sheet_rate_limited")

    monkeypatch.setattr(career_sheets, "_list_records_impl", boom)
    with pytest.raises(CareerToolError) as excinfo:
        career_sheets.query_career_sheet_records(
            _context(), QueryCareerSheetRecordsInput(company_keywords=["腾讯"])
        )
    assert excinfo.value.code == "sheet_rate_limited"
    assert "search-public-job-pages" in excinfo.value.message


def test_sheet_bridge_unavailable_yields_stable_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(file_id: str, sheet_id: str, limit: int, offset: int) -> dict[str, Any]:
        del file_id, sheet_id, limit, offset
        raise career_sheets.SheetQueryError("sheet_bridge_unavailable")

    monkeypatch.setattr(career_sheets, "_list_records_impl", boom)
    with pytest.raises(CareerToolError) as excinfo:
        career_sheets.query_career_sheet_records(
            _context(), QueryCareerSheetRecordsInput(role_keywords=["算法"])
        )
    assert excinfo.value.code == "sheet_bridge_unavailable"
