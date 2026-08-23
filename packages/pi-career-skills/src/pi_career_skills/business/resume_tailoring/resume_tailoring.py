"""Fact-grounded tailoring brief for the PEV ``resume-tailoring`` Skill."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

from pi_career_skills.business.job_discovery.target_evidence import (
    resolve_target_evidence,
)
from pydantic import BaseModel, Field, field_validator

from pi_career_skills.context import ToolContext


class ResumeTailoringError(RuntimeError):
    """Stable, non-sensitive resume-tailoring failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BuildResumeTailoringBriefInput(BaseModel):
    """One evidence-backed target JD and the terms the Agent wants checked."""

    target_artifact_id: str = Field(min_length=1, max_length=80)
    target_keywords: list[str] = Field(min_length=1, max_length=30)

    @field_validator("target_keywords")
    @classmethod
    def normalize_keywords(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            display_value = value.strip()
            normalized_value = display_value.lower()
            if display_value and normalized_value not in seen:
                seen.add(normalized_value)
                cleaned.append(display_value)
        if not cleaned:
            raise ValueError("target_keywords must include a non-empty value")
        return cleaned


class ResumeTailoringBriefOutput(BaseModel):
    """Fact-grounded resume changes that await user review before application."""

    target_artifact_id: str
    target_title: str | None
    source_url: str
    source_attribution: str | None = None
    supported_keywords: list[str]
    missing_keywords: list[str]
    safe_actions: list[str]
    proposed_diffs: list[ResumeTailoringDiff]


class ResumeTailoringDiff(BaseModel):
    """One reviewable operation grounded in both a fact field and selected JD."""

    op: Literal["highlight", "reorder"]
    section: str
    fact_ref: str
    target_evidence_ref: str
    change_summary: str


def build_resume_tailoring_brief(
    context: ToolContext, payload: BuildResumeTailoringBriefInput
) -> ResumeTailoringBriefOutput:
    """Compare only one observed JD with facts already confirmed by the user."""
    target = resolve_target_evidence(
        context.metadata.get("observed_public_evidence"),
        context.metadata.get("structured_job_candidates"),
        payload.target_artifact_id,
    )
    if target is None:
        raise ResumeTailoringError("target_evidence_not_found")
    visible_text = target.get("visible_text")
    if isinstance(visible_text, str) and visible_text.strip():
        source_url = target.get("source_url")
        if not isinstance(source_url, str):
            raise ResumeTailoringError("target_evidence_incomplete")
        target_title = target.get("title") if isinstance(target.get("title"), str) else None
        job_text = f"{target_title or ''}\n{visible_text}".lower()
        if not _target_source_matches_goal(
            context.metadata.get("task_goal"), source_url, job_text
        ):
            raise ResumeTailoringError("target_source_mismatch")
        if not _target_matches_goal(context.metadata.get("task_goal"), job_text):
            raise ResumeTailoringError("target_role_mismatch")
    else:
        raise ResumeTailoringError("target_evidence_incomplete")
    required_keywords = [
        (keyword, keyword.lower())
        for keyword in payload.target_keywords
        if keyword.lower() in job_text
    ]
    confirmed_text = _flatten_text(context.metadata.get("confirmed_profile_facts")).lower()
    confirmed_facts = context.metadata.get("confirmed_profile_facts")
    supported_pairs = [
        pair for pair in required_keywords if pair[1] in confirmed_text
    ]
    missing_pairs = [
        pair for pair in required_keywords if pair[1] not in confirmed_text
    ]
    supported = [normalized for _display, normalized in supported_pairs]
    missing = [normalized for _display, normalized in missing_pairs]
    actions: list[str] = []
    if supported:
        actions.append(
            "在项目经历中优先展示已确认的 "
            f"{'、'.join(display for display, _normalized in supported_pairs)} 事实，并量化可核验结果。"
        )
    if missing:
        actions.append(
            f"{'、'.join(display for display, _normalized in missing_pairs)} 尚无已确认事实：仅在能补充项目证据时添加，不得虚构。"
        )
    proposed_diffs = [
        ResumeTailoringDiff(
            op="highlight",
            section=_resume_section_for_fact(fact_ref),
            fact_ref=fact_ref,
            target_evidence_ref=payload.target_artifact_id,
            change_summary=(
                f"将已确认的 {display} 事实前置到"
                f"{_resume_section_label(_resume_section_for_fact(fact_ref))}部分，并保留原有可核验表述。"
            ),
        )
        for display, normalized in supported_pairs
        if (fact_ref := _find_fact_ref_for_keyword(confirmed_facts, normalized))
        is not None
    ]
    return ResumeTailoringBriefOutput(
        target_artifact_id=payload.target_artifact_id,
        target_title=target_title,
        source_url=source_url,
        source_attribution=_source_attribution(source_url, job_text),
        supported_keywords=supported,
        missing_keywords=missing,
        safe_actions=actions,
        proposed_diffs=proposed_diffs,
    )


def _find_target(raw_evidence: object, artifact_id: str) -> dict[str, Any] | None:
    if not isinstance(raw_evidence, list):
        return None
    for item in raw_evidence:
        if isinstance(item, dict) and item.get("artifact_id") == artifact_id:
            return item
    return None


def _target_matches_goal(goal: object, searchable: str) -> bool:
    """Reject a model-selected JD that is clearly for another requested role."""
    if not isinstance(goal, str) or not goal.strip():
        return True
    role_groups = (
        (("产品经理", "产品类", "aigc"), ("产品经理", "aigc")),
        (("大模型应用开发", "llm 应用", "llm应用"), ("大模型", "应用开发", "llm", "agent")),
        (("前端开发",), ("前端", "frontend")),
        (("java 后端", "java后端"), ("java", "后端")),
    )
    lowered_goal = goal.lower()
    if any(marker in lowered_goal for marker in ("应届生", "应届", "校招", "实习生")) and not any(
        marker in searchable
        for marker in (
            "应届生", "应届", "校招", "校园招聘", "毕业生", "campus",
            "graduate", "实习生", "实习", "intern",
        )
    ):
        return False
    for markers, evidence_terms in role_groups:
        if any(marker in lowered_goal for marker in markers):
            if "产品经理" in markers:
                if "产品经理" in lowered_goal and "产品经理" not in searchable:
                    return False
                if "aigc" in lowered_goal:
                    return any(
                        marker in searchable
                        for marker in (
                            "aigc",
                            "生成式ai",
                            "生成式人工智能",
                            "大模型",
                            "llm",
                            "prompt",
                            "rag",
                            "ai agent",
                        )
                    )
                if "产品经理" not in lowered_goal:
                    return "aigc" in searchable
                return True
            return any(term in searchable for term in evidence_terms)
    return True


def _source_attribution(source_url: str, job_text: str) -> str | None:
    """Return only an attribution stated by the captured source evidence."""
    host = (urlsplit(source_url).hostname or "").lower().rstrip(".")
    if host == "liepin.com" or host.endswith(".liepin.com"):
        return "猎聘"
    if "该职位来源于猎聘" in job_text:
        return "猎聘"
    return None


def _target_source_matches_goal(
    goal: object, source_url: str, job_text: str
) -> bool:
    """Enforce an explicitly named source using only captured attribution."""
    if not isinstance(goal, str) or "猎聘" not in goal:
        return True
    return _source_attribution(source_url, job_text) == "猎聘"


def _structured_target_evidence(
    candidates: object, target: dict[str, Any], artifact_id: str
) -> tuple[str | None, str | None, str | None]:
    """Resolve a collapsed target pointer to full JD text via structured candidates.

    ``observed_public_evidence`` entries that fall outside the decision
    projection budget collapse to identifier-only lines (``artifact_id`` /
    ``source_url``, never ``visible_text``). The run's structured extraction
    candidates retain the full JD text, so the pointer is resolved by matching
    ``artifact_id`` (the candidate's own or the evidence artifact it was
    derived from) or ``source_url``. Returns ``(title, job_text, source_url)``,
    or ``(None, None, None)`` when no candidate yields usable text - the caller
    keeps raising ``target_evidence_incomplete``.
    """
    if not isinstance(candidates, list):
        return None, None, None
    target_source_url = target.get("source_url")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if not (
            artifact_id == candidate.get("artifact_id")
            or artifact_id == candidate.get("source_artifact_id")
            or (
                isinstance(target_source_url, str)
                and target_source_url == candidate.get("source_url")
            )
        ):
            continue
        source_url = candidate.get("source_url")
        if not isinstance(source_url, str) or not source_url:
            return None, None, None
        title = candidate.get("title")
        job_text = candidate.get("full_text")
        if not isinstance(job_text, str) or not job_text.strip():
            job_text = _candidate_section_text(candidate)
        if not isinstance(job_text, str) or not job_text.strip():
            return None, None, None
        return (
            title if isinstance(title, str) else None,
            job_text,
            source_url,
        )
    return None, None, None


def _candidate_section_text(candidate: dict[str, Any]) -> str | None:
    """Concatenate a structured candidate's sections as last-resort job text."""
    sections = [
        candidate.get("title"),
        candidate.get("company_name"),
        candidate.get("responsibilities"),
        candidate.get("requirements"),
    ]
    text = "\n".join(section for section in sections if isinstance(section, str) and section)
    return text or None


def _flatten_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_flatten_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_flatten_text(item) for item in value)
    return ""


def _find_fact_ref_for_keyword(facts: object, keyword: str) -> str | None:
    """Choose the first confirmed top-level fact field containing one JD term."""
    if not isinstance(facts, dict):
        return None
    for field_path, value in facts.items():
        if isinstance(field_path, str) and keyword in _flatten_text(value).lower():
            return field_path
    return None


def _resume_section_for_fact(fact_ref: str) -> str:
    """Map known profile fields to a user-visible resume section without inference."""
    if fact_ref in {"skills", "languages", "certificates"}:
        return "skills"
    if fact_ref in {"projects", "project", "experience"}:
        return "projects"
    return "summary"


def _resume_section_label(section: str) -> str:
    return {"skills": "技能", "projects": "项目经历", "summary": "个人概述"}[section]
