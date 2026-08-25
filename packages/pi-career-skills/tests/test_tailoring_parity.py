"""Golden parity tests for resume-tailoring pure functions.

Verifies byte-level equivalence against source-project golden fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pi_career_skills.business.resume_tailoring.keywords import (
    goal_role_keywords,
    tailoring_keywords,
)
from pi_career_skills.business.resume_tailoring.resume_tailoring import (
    BuildResumeTailoringBriefInput,
    ResumeTailoringError,
    build_resume_tailoring_brief,
)
from pi_career_skills.context import ToolContext

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _ctx(metadata: dict) -> ToolContext:
    return ToolContext(user_id="u_fixture", run_id="r_fixture", metadata=metadata)


# ---------------------------------------------------------------------------
# Golden parity: build_resume_tailoring_brief
# ---------------------------------------------------------------------------

def test_golden_tailoring_brief_parity() -> None:
    """Output of build_resume_tailoring_brief must match tailoring_brief.json exactly."""
    evidence = [
        {
            "artifact_id": "art1",
            "source_url": "https://yunqi.example.com/jobs/frontend",
            "quality": "jd_complete",
            "visible_text": (
                "前端开发工程师 岗位职责 负责 Vue3 TypeScript 开发 "
                "任职要求 熟悉 Vue3 TypeScript Vite 2 年经验"
            ),
        }
    ]
    result = build_resume_tailoring_brief(
        _ctx(
            {
                "observed_public_evidence": evidence,
                "confirmed_profile_facts": [
                    {"field": "name", "value": "张三"},
                    {"field": "skill", "value": "Vue3"},
                    {"field": "skill", "value": "TypeScript"},
                ],
                "task_goal": (
                    "针对岗位 前端开发工程师 定制简历，"
                    "突出 Vue3、TypeScript、Vite 经历"
                ),
            }
        ),
        BuildResumeTailoringBriefInput(
            target_artifact_id="art1",
            target_keywords=["Vue3", "TypeScript", "Vite", "前端"],
        ),
    )

    golden = json.loads((FIXTURE_DIR / "tailoring_brief.json").read_text("utf-8"))
    actual = result.model_dump(mode="json")

    # Field-by-field equality (golden keys are sorted alphabetically)
    assert sorted(actual.keys()) == sorted(golden.keys()), (
        "key set mismatch"
    )
    for key in golden:
        assert actual[key] == golden[key], f"field '{key}' mismatch"


# ---------------------------------------------------------------------------
# Direct tests: edge paths
# ---------------------------------------------------------------------------

def test_no_confirmed_facts_yields_safe_actions_only() -> None:
    """Without confirmed facts, supported_keywords is empty and no diffs fabricated."""
    evidence = [
        {
            "artifact_id": "art2",
            "source_url": "https://yunqi.example.com/jobs/frontend",
            "quality": "jd_complete",
            "visible_text": "前端开发工程师 熟悉 Vue3 TypeScript",
        }
    ]
    result = build_resume_tailoring_brief(
        _ctx(
            {
                "observed_public_evidence": evidence,
                "confirmed_profile_facts": {},
                "task_goal": "前端开发",
            }
        ),
        BuildResumeTailoringBriefInput(
            target_artifact_id="art2",
            target_keywords=["Vue3", "TypeScript"],
        ),
    )
    assert result.supported_keywords == []
    assert result.proposed_diffs == []
    assert result.missing_keywords == ["vue3", "typescript"]
    # Only the "missing facts" safe action, not the "supported" one
    assert len(result.safe_actions) == 1
    assert "尚无已确认事实" in result.safe_actions[0]


def test_unmatched_keywords_still_materialize_reviewable_brief() -> None:
    evidence = [
        {
            "artifact_id": "art3",
            "source_url": "https://yunqi.example.com/jobs/agent",
            "quality": "jd_complete",
            "visible_text": "AI Agent 开发工程师 负责智能体应用开发",
        }
    ]
    result = build_resume_tailoring_brief(
        _ctx(
            {
                "observed_public_evidence": evidence,
                "confirmed_profile_facts": {"skills": ["Python"]},
                "task_goal": "AI Agent 开发工程师",
            }
        ),
        BuildResumeTailoringBriefInput(
            target_artifact_id="art3", target_keywords=["Kubernetes"]
        ),
    )
    assert result.safe_actions
    assert "未出现" in result.safe_actions[0]


def test_tailoring_persists_canonical_id_for_observed_selector() -> None:
    evidence = [
        {
            "artifact_id": "canonical-jd",
            "content_hash": "a" * 64,
            "source_url": "https://example.com/jobs/frontend",
            "visible_text": "前端开发工程师 熟悉 Vue3",
        }
    ]
    result = build_resume_tailoring_brief(
        _ctx(
            {
                "observed_public_evidence": evidence,
                "confirmed_profile_facts": {"skills": ["Vue3"]},
                "task_goal": "前端开发工程师",
            }
        ),
        BuildResumeTailoringBriefInput(
            target_artifact_id="observed:" + ("a" * 64),
            target_keywords=["Vue3"],
        ),
    )
    assert result.target_artifact_id == "canonical-jd"
    assert result.proposed_diffs[0].target_evidence_ref == "canonical-jd"


def test_target_artifact_missing_raises_not_found() -> None:
    """When the target artifact_id is absent from evidence, raise target_evidence_not_found."""
    evidence = [
        {
            "artifact_id": "art_exist",
            "source_url": "https://yunqi.example.com/jobs/frontend",
            "quality": "jd_complete",
            "visible_text": "前端开发工程师",
        }
    ]
    with pytest.raises(ResumeTailoringError) as exc_info:
        build_resume_tailoring_brief(
            _ctx({"observed_public_evidence": evidence, "task_goal": "前端开发"}),
            BuildResumeTailoringBriefInput(
                target_artifact_id="art_missing",
                target_keywords=["Vue3"],
            ),
        )
    assert exc_info.value.code == "target_evidence_not_found"


# ---------------------------------------------------------------------------
# Direct tests: keywords helpers
# ---------------------------------------------------------------------------

def test_goal_role_keywords_frontend() -> None:
    assert goal_role_keywords("前端开发工程师") == ["前端", "Frontend", "Vue"]


def test_goal_role_keywords_fallback() -> None:
    assert goal_role_keywords("随便一个岗位") == ["岗位"]


def test_tailoring_keywords_from_candidate_and_facts() -> None:
    candidate = {
        "title": "Java 后端开发",
        "responsibilities": "负责后端服务",
        "requirements": "Java 后端经验",
    }
    confirmed = {"skills": ["Spring", "MySQL"]}
    keywords = tailoring_keywords("随便一个岗位", confirmed, candidate)
    # Candidate markers + confirmed skills, deduplicated, order preserved
    assert keywords == ["Java", "后端", "Spring", "MySQL"]
