"""Unit tests for polite public-request governance."""

from __future__ import annotations

import pytest

from pi_career_skills.business.job_discovery.models import FetchPublicJobPageOutput
from pi_career_skills.context import ToolContext
from pi_career_skills.errors import CareerToolError
from pi_career_skills.network import request_governor


def _context() -> ToolContext:
    return ToolContext(
        user_id="u",
        run_id="r",
        metadata={
            "enforce_public_request_governor": True,
            "public_page_cache_ttl_seconds": 600,
            "public_block_cooldown_seconds": 600,
        },
    )


def _page(url: str) -> FetchPublicJobPageOutput:
    return FetchPublicJobPageOutput(
        artifact_id="observed:" + "a" * 64,
        source_url=url,
        title="AI Agent 开发工程师",
        visible_text="岗位职责：负责 AI Agent 应用开发。任职要求：熟悉 Python 和 RAG。",
        content_hash="a" * 64,
        effective_url=url,
        http_status=200,
        quality="jd_complete",
    )


def test_canonical_request_url_normalizes_query_and_fragment() -> None:
    assert request_governor.canonical_request_url(
        "HTTPS://Jobs.Example.com/post/?b=2&a=1#section"
    ) == "https://jobs.example.com/post?a=1&b=2"


def test_page_cache_reuses_equivalent_urls() -> None:
    request_governor.clear_for_tests()
    context = _context()
    request_governor.put_cached_page(
        context, _page("https://jobs.example.com/post/?b=2&a=1")
    )
    cached = request_governor.get_cached_page(
        context, "https://jobs.example.com/post?a=1&b=2#top"
    )
    assert cached is not None
    assert cached.content_hash == "a" * 64
    request_governor.clear_for_tests()


def test_blocked_domain_cooldown_is_process_wide() -> None:
    request_governor.clear_for_tests()
    context = _context()
    request_governor.remember_blocked(
        context, "https://jobs.example.com/post/1", "anti_bot_challenge"
    )
    with pytest.raises(CareerToolError) as excinfo:
        request_governor.ensure_available(
            _context(), "https://jobs.example.com/post/2"
        )
    assert excinfo.value.code == "domain_temporarily_blocked"
    request_governor.clear_for_tests()
