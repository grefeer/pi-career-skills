"""Golden parity tests for job_matching pure functions.

Verifies that the pi-career-skills port of match_observed_jobs produces
byte-equivalent output to the source DeepAgents career-assistant implementation.
"""

from __future__ import annotations

import json
from pathlib import Path

from pi_career_skills.business.job_matching.job_matching import (
    MatchObservedJobsInput,
    _candidate_recency_verified,
    match_observed_jobs,
)
from pi_career_skills.context import ToolContext

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Fixture inputs (verbatim from scripts/gen_pi_golden_fixtures.py)
# ---------------------------------------------------------------------------


def _ctx(metadata: dict) -> ToolContext:
    return ToolContext(user_id="u_fixture", run_id="r_fixture", metadata=metadata)


def _structured_candidates() -> list[dict]:
    return [
        {
            "candidate_id": "art1:candidate:0",
            "artifact_id": "art1",
            "source_url": "https://yunqi.example.com/jobs/frontend",
            "title": "前端开发工程师",
            "company_name": "深圳云启科技",
            "locations": ["深圳"],
            "responsibilities": "负责 Vue3、TypeScript 项目开发",
            "requirements": "2 年以上前端经验，熟悉 Vue3、TypeScript、Vite",
            "description_text": "前端开发工程师 负责 Vue3 TypeScript 开发 2 年经验",
            "recruitment_types": ["social"],
            "published_at": "2026-08-20",
        }
    ]


def _raw_evidence() -> list[dict]:
    return [
        {
            "artifact_id": "raw1",
            "source_url": "https://liepin.example.com/jobs/frontend",
            "quality": "jd_complete",
            "visible_text": "前端开发工程师 深圳 负责 Vue3 TypeScript 开发 任职要求 2 年经验",
        }
    ]


# ---------------------------------------------------------------------------
# Golden parity tests
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def test_match_observed_structured_candidates_parity():
    """match_observed_jobs over structured candidates matches golden fixture."""
    result = match_observed_jobs(
        _ctx({"structured_job_candidates": _structured_candidates()}),
        MatchObservedJobsInput(
            profile_keywords=["Vue3", "TypeScript", "Vite"],
            preferred_locations=["深圳"],
        ),
    )
    golden = _load_fixture("match_observed.json")
    assert result.model_dump(mode="json") == golden


def test_match_raw_evidence_parity():
    """match_observed_jobs over raw observed evidence matches golden fixture."""
    result = match_observed_jobs(
        _ctx({"observed_public_evidence": _raw_evidence()}),
        MatchObservedJobsInput(
            profile_keywords=["Vue3"],
            preferred_locations=["深圳"],
        ),
    )
    golden = _load_fixture("match_raw_evidence.json")
    assert result.model_dump(mode="json") == golden


# ---------------------------------------------------------------------------
# Direct behavior tests
# ---------------------------------------------------------------------------


def test_empty_candidates_returns_empty_matches():
    """No structured candidates and no raw evidence -> empty matches."""
    result = match_observed_jobs(
        _ctx({}),
        MatchObservedJobsInput(profile_keywords=["python"]),
    )
    assert result.matches == []
    assert result.evaluated_candidate_count == 0
    assert result.no_match_reason is None


def test_no_matching_keywords_sets_no_match_reason():
    """Candidates exist but none satisfy constraints -> no_match_reason set."""
    candidates = [
        {
            "candidate_id": "art1:candidate:0",
            "artifact_id": "art1",
            "source_url": "https://example.com/jobs/marketing",
            "title": "市场专员",
            "company_name": "示例公司",
            "locations": ["北京"],
            "responsibilities": "负责品牌推广与活动策划",
            "requirements": "2 年以上市场经验",
        }
    ]
    result = match_observed_jobs(
        _ctx({
            "structured_job_candidates": candidates,
            "task_goal": "Java 后端开发，工作地点深圳",
        }),
        MatchObservedJobsInput(
            profile_keywords=["java", "spring"],
            preferred_locations=["深圳"],
        ),
    )
    assert result.evaluated_candidate_count == 1
    assert result.matches == []
    assert result.no_match_reason == "no_candidate_satisfied_constraints"


def test_raw_jd_fallback_when_structured_seed_is_unrelated() -> None:
    """A bad structured seed must not hide a valid persisted public JD."""
    result = match_observed_jobs(
        _ctx(
            {
                "task_goal": "AI Agent 开发工程师，应届生",
                "structured_job_candidates": [
                    {
                        "candidate_id": "seed:candidate:0",
                        "artifact_id": "seed",
                        "source_url": "https://example.com/list",
                        "title": "市场专员",
                        "responsibilities": "品牌活动",
                        "requirements": "市场经验",
                    }
                ],
                "observed_public_evidence": [
                    {
                        "artifact_id": "jd1",
                        "source_url": "https://example.com/jobs/agent",
                        "title": "AI 应用开发工程师",
                        "quality": "jd_complete",
                        "visible_text": (
                            "AI Agent 应用开发工程师，负责智能体应用开发，"
                            "要求 Python、RAG，面向应届生。"
                        ),
                    }
                ],
            }
        ),
        MatchObservedJobsInput(profile_keywords=["python", "rag"]),
    )
    assert result.matches
    assert result.matches[0].artifact_id == "jd1"


def test_recency_reads_explicit_publication_date_from_visible_page_text():
    """Static campus pages may carry the labelled date only in visible text."""
    candidate = {
        "visible_text": "岗位信息\n发布时间：2026-08-24\n职位描述：AI 应用开发",
    }
    assert _candidate_recency_verified(candidate, "最近7天")


def test_matched_keywords_are_lowercased():
    """profile_keywords are normalized to lowercase before matching."""
    candidates = [
        {
            "candidate_id": "art1:candidate:0",
            "artifact_id": "art1",
            "source_url": "https://example.com/jobs/frontend",
            "title": "前端工程师",
            "responsibilities": "使用 Vue3 开发",
            "requirements": "熟悉 TypeScript",
        }
    ]
    result = match_observed_jobs(
        _ctx({"structured_job_candidates": candidates}),
        MatchObservedJobsInput(profile_keywords=["VUE3", "TYPESCRIPT"]),
    )
    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.matched_keywords == ["vue3", "typescript"]
    # All matched keywords are lowercase
    assert all(kw == kw.lower() for kw in match.matched_keywords)
