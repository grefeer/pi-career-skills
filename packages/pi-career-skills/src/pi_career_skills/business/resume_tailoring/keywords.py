"""Resume-tailoring keyword derivation from a user goal."""

from __future__ import annotations

from typing import Any


def goal_role_keywords(goal: str) -> list[str]:
    """Derive bounded role keywords for tailoring target selection."""
    lowered = goal.lower()
    if "产品经理" in lowered or "aigc" in lowered:
        return ["产品经理", "AIGC", "AI"]
    if "ai 应用开发" in lowered or "ai应用开发" in lowered:
        return ["AI", "应用开发", "Agent", "智能体"]
    if "大模型应用开发" in lowered or "llm 应用" in lowered or "llm应用" in lowered:
        return ["大模型", "应用开发", "Agent", "AI"]
    if "前端开发" in lowered:
        return ["前端", "Frontend", "Vue"]
    if "java 后端" in lowered or "java后端" in lowered:
        return ["Java", "后端"]
    return ["岗位"]


def tailoring_keywords(
    goal: str, confirmed_facts: Any, candidate: dict[str, Any]
) -> list[str]:
    """Derive target keywords for a tailoring brief from goal + confirmed facts."""
    keywords = goal_role_keywords(goal)
    if keywords != ["岗位"]:
        return keywords
    text = " ".join(
        str(candidate.get(key) or "")
        for key in ("title", "responsibilities", "requirements")
    )
    inferred = [
        marker
        for marker in (
            "产品经理",
            "前端",
            "Java",
            "后端",
            "大模型",
            "AIGC",
            "AI",
            "Agent",
            "RAG",
        )
        if marker.lower() in text.lower()
    ]
    if isinstance(confirmed_facts, dict) and isinstance(
        confirmed_facts.get("skills"), list
    ):
        inferred.extend(
            skill for skill in confirmed_facts["skills"] if isinstance(skill, str)
        )
    return list(dict.fromkeys(inferred)) or ["岗位"]
