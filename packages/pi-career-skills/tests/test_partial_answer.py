"""Tests for the partial-answer renderer (non-succeeded runs still answer).

Covers: sheet/search records rendering with source links, JD-page rendering,
empty-store returns None, lead cap, and error-code notes.
"""

from __future__ import annotations

import hashlib

from pi_career_skills.contracts import ToolObservation
from pi_career_skills.runtime.evidence import EvidenceStore
from pi_career_skills.runtime.partial_answer import build_partial_answer


def _make_hash(value: dict) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _sheet_obs(records: list[dict]) -> ToolObservation:
    output = {
        "source_url": "https://docs.qq.com/sheet/ABC",
        "content_hash": _make_hash({"s": len(records)}),
        "records": records,
        "matched_count": len(records),
        "scanned_count": 100,
        "sheets_queried": 4,
        "truncated": False,
        "query": {"role_keywords": ["AI"]},
    }
    return ToolObservation(
        tool_name="query-career-sheet-records",
        status="succeeded",
        output=output,
    )


def _page_obs(quality: str) -> ToolObservation:
    output = {
        "source_url": "https://example.com/jobs/1",
        "content_hash": _make_hash({"q": quality}),
        "quality": quality,
        "title": "AI 应用开发工程师",
        "company_name": "腾讯",
        "locations": ["深圳", "北京"],
        "responsibilities": "负责大模型 Agent 平台研发",
    }
    return ToolObservation(
        tool_name="fetch-public-job-page",
        status="succeeded",
        output=output,
    )


def _empty_store() -> EvidenceStore:
    return EvidenceStore()


def _sheet_store(records: list[dict]) -> EvidenceStore:
    store = EvidenceStore()
    store.add_observation(_sheet_obs(records))
    return store


def test_empty_store_returns_none() -> None:
    assert build_partial_answer(_empty_store(), error_code="budget_exhausted") is None


def test_sheet_records_rendered_with_links_and_referral() -> None:
    store = _sheet_store(
        [
            {
                "company_name": "腾讯",
                "apply_url": "https://join.qq.com/pos/1",
                "industry": "互联网",
                "location": "深圳",
                "recruitment_type": "内推",
                "updated_at": "2026-08-28",
                "raw_summary": "AI 应用开发工程师，负责大模型 Agent 平台研发",
                "prior_metadata": {"referral_code": "ABC123"},
            },
            {
                "company_name": "字节跳动",
                "apply_url": "https://jobs.bytedance.com/pos/2",
                "industry": "互联网",
                "location": "北京",
                "recruitment_type": "校招",
                "updated_at": "2026-08-27",
                "raw_summary": "",
            },
        ]
    )
    text = build_partial_answer(store, error_code="budget_exhausted")
    assert text is not None
    assert "预算耗尽" in text  # error-code note surfaced
    assert "部分完成" in text  # never presented as a completed deliverable
    assert "腾讯" in text and "https://join.qq.com/pos/1" in text
    assert "内推码 ABC123" in text
    assert "字节跳动" in text and "https://jobs.bytedance.com/pos/2" in text
    assert "2 条岗位线索" in text


def test_jd_page_rendered() -> None:
    store = EvidenceStore()
    store.add_observation(_page_obs("jd_complete"))
    text = build_partial_answer(store, error_code="completion_evidence_unavailable")
    assert text is not None
    assert "AI 应用开发工程师" in text
    assert "腾讯" in text
    assert "https://example.com/jobs/1" in text
    assert "证据未达到完成门槛" in text


def test_unknown_error_code_uses_default_note() -> None:
    text = build_partial_answer(_sheet_store([{
        "company_name": "腾讯",
        "apply_url": "https://join.qq.com/pos/1",
    }]), error_code="some_unknown_code")
    assert text is not None
    assert "运行未完成" in text


def test_lead_cap_limits_records() -> None:
    records = [
        {
            "company_name": f"公司{i}",
            "apply_url": f"https://jobs.example.com/pos/{i}",
        }
        for i in range(25)
    ]
    text = build_partial_answer(_sheet_store(records), error_code="budget_exhausted")
    assert text is not None
    assert "公司0" in text
    assert "公司19" in text
    assert "公司24" not in text  # capped at _MAX_LEADS
    assert "20 条岗位线索" in text
