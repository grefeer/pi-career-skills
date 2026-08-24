"""Tests for evaluation.profile_facts — source-parity profile → facts extraction."""

from __future__ import annotations

import json
from pathlib import Path

from pi_career_skills.evaluation.profile_facts import build_profile_facts

# ---------------------------------------------------------------------------
# Helper — load sample profiles from source question files
# ---------------------------------------------------------------------------

SOURCE_ROOT = Path(
    r"d:\Program Files\JetBrains\PyCharm Community Edition 2024.2.2"
    r"\proj\langgraph-multi-agent-career-assistant-main"
)
QUESTION_DIR = SOURCE_ROOT / "tests" / "question" / "redesign"


def _load_profile(qid: str) -> dict:
    """Load the profile from a source question JSON file."""
    data = json.loads((QUESTION_DIR / f"{qid}.json").read_text(encoding="utf-8"))
    return data["profile"]


def _load_chain_link_profile(qid: str, link_index: int = 0) -> dict:
    """Load a link's profile from a chain question JSON file."""
    data = json.loads((QUESTION_DIR / f"{qid}.json").read_text(encoding="utf-8"))
    return data["chain"][link_index]["profile"]


# ====================================================================
# Synthetic profile (no resume_text) — P1-P4 samples
# ====================================================================


def test_synthetic_p3_frontend_2yr() -> None:
    """Q011 profile P3 — 前端开发工程师（2 年经验）, Vue3/TS/Vite."""
    profile = _load_profile("Q011")
    assert "resume_text" not in profile or not profile.get("resume_text")
    facts = build_profile_facts(profile)

    # basics.name = role + （评测画像）
    assert "basics.name" in facts
    assert facts["basics.name"] == "前端开发工程师（2 年经验）（评测画像）"

    # education section → 社招（2 年经验）
    assert "education" in facts
    assert facts["education"] == ["社招（2 年经验）"]

    # skills section → split by 、
    assert "skills" in facts
    skills = facts["skills"]
    assert isinstance(skills, list)
    assert "Vue3" in skills
    assert "TypeScript" in skills
    assert "Vite" in skills


def test_synthetic_p1_ai_freshgrad() -> None:
    """Q013 profile P1 — AI 应用开发工程师（应届生）, Python/LangChain/RAG/Agent."""
    profile = _load_profile("Q013")
    facts = build_profile_facts(profile)

    assert facts["basics.name"] == "AI 应用开发工程师（应届生）（评测画像）"
    assert facts["education"] == ["应届生（校招）"]
    skills = facts["skills"]
    assert isinstance(skills, list)
    assert "Python" in skills
    assert "LangChain" in skills
    assert "RAG" in skills
    assert "Agent" in skills


def test_synthetic_empty_skills_still_works() -> None:
    """Odd summary with no skills match — should not crash."""
    profile = {
        "role": "测试工程师",
        "summary": "随便写点什么，没有技能关键词",
    }
    facts = build_profile_facts(profile)
    # name always present (first line ≤120 char)
    assert "basics.name" in facts
    # skills might be empty list if no match
    # (depends on whether 技能 section header is generated — yes it is, with empty line)


def test_synthetic_empty_experience() -> None:
    """Summary with neither 应届生 nor 社招 pattern."""
    profile = {
        "role": "自由职业者",
        "summary": "技能：Python、Go，其他没了",
    }
    facts = build_profile_facts(profile)
    assert "basics.name" in facts
    skills = facts["skills"]
    assert isinstance(skills, list)
    assert "Python" in skills
    assert "Go" in skills


# ====================================================================
# Full resume_text (R1 sample)
# ====================================================================


def test_resume_text_r1_full() -> None:
    """C001 link0 profile R1 — full resume text with all sections."""
    profile = _load_chain_link_profile("C001", 0)
    assert profile.get("resume_text"), "R1 profile must have resume_text"
    facts = build_profile_facts(profile)

    # basics
    assert "basics.name" in facts
    assert facts["basics.name"] == "高硕谦"
    assert "basics.email" in facts
    assert facts["basics.email"] == "815733200@qq.com"
    assert "basics.phone" in facts
    assert facts["basics.phone"] == "18931037861"

    # sections present
    assert "education" in facts
    assert "experience" in facts
    assert "projects" in facts
    assert "skills" in facts
    assert "certificates" in facts

    # skills list has expected entries
    skills = facts["skills"]
    assert isinstance(skills, list)
    assert "Python" in skills
    assert "PyTorch" in skills
    assert "LangChain" in skills
    assert "RAG" in skills
    assert "Docker" in skills

    # education has content
    edu = facts["education"]
    assert isinstance(edu, list)
    assert len(edu) >= 2  # at least 研究生 + 本科


# ====================================================================
# Determinism
# ====================================================================


def test_build_profile_facts_deterministic() -> None:
    profile = _load_profile("Q011")
    a = build_profile_facts(profile)
    b = build_profile_facts(profile)
    assert a == b


def test_resume_text_deterministic() -> None:
    profile = _load_chain_link_profile("C001", 0)
    a = build_profile_facts(profile)
    b = build_profile_facts(profile)
    assert a == b


# ====================================================================
# Edge cases
# ====================================================================


def test_empty_resume_text_is_not_treated_as_present() -> None:
    profile = {"role": "X", "summary": "技能：Python", "resume_text": ""}
    facts = build_profile_facts(profile)
    # Empty resume_text → falls through to synthetic path
    # Should still produce basics.name from synthetic
    assert "basics.name" in facts


def test_none_resume_text_falls_through() -> None:
    profile = {"role": "X", "summary": "技能：Python", "resume_text": None}
    facts = build_profile_facts(profile)
    assert "basics.name" in facts


def test_minimal_profile_with_only_role_summary() -> None:
    profile = {"role": "测试岗", "summary": "社招（5 年经验），技能：测试"}
    facts = build_profile_facts(profile)
    assert facts["basics.name"] == "测试岗（评测画像）"
    assert facts["education"] == ["社招（5 年经验）"]
    assert "测试" in facts["skills"]
