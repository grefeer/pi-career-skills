"""JD-grounded preparation planning for the ``career-planning`` skill."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from pi_career_skills.business.common.skill_validator import normalize_skill, skills_from_text
from pi_career_skills.business.job_discovery.target_evidence import resolve_target_evidence
from pi_career_skills.context import ToolContext
from pi_career_skills.errors import CareerToolError


class CareerPlanningError(CareerToolError):
    """Stable, non-sensitive career-planning failure."""


class BuildPreparationPlanInput(BaseModel):
    """One persisted JD plus user-selected topics to validate against it."""

    target_artifact_id: str = Field(min_length=1, max_length=80)
    focus_keywords: list[str] = Field(min_length=1, max_length=30)
    time_budget_hours: int = Field(default=6, ge=1, le=80)
    target_date: date | None = None
    additional_target_artifact_ids: list[str] = Field(default_factory=list, max_length=20)
    resume_skills: list[str] = Field(default_factory=list, max_length=50)
    gap_limit: int = Field(default=5, ge=1, le=20)

    @field_validator("focus_keywords")
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
            raise ValueError("focus_keywords must include a non-empty value")
        return cleaned


class SkillGap(BaseModel):
    """One missing closed-set skill ranked across selected observed JDs."""

    skill: str
    job_count: int


class PreparationSchedule(BaseModel):
    """A user deadline or an intentionally non-calendar relative schedule."""

    kind: Literal["target_date", "relative"]
    target_date: date | None = None
    target_date_provenance: Literal["user_supplied"] | None = None
    relative_window: str | None = None

    @model_validator(mode="after")
    def validate_schedule_variant(self) -> PreparationSchedule:
        """Reject deadline fields that contradict the schedule's declared kind."""
        if self.kind == "relative":
            if (
                self.target_date is not None
                or self.target_date_provenance is not None
                or not isinstance(self.relative_window, str)
                or not self.relative_window.strip()
            ):
                raise ValueError(
                    "relative schedule requires no target date/provenance and a relative window"
                )
        elif (
            self.target_date is None
            or self.target_date_provenance != "user_supplied"
            or self.relative_window is not None
        ):
            raise ValueError(
                "target-date schedule requires a user-supplied date and no relative window"
            )
        return self


class PreparationPlanItem(BaseModel):
    """One bounded, reviewable action supported by the selected JD."""

    topic: str
    priority: Literal["P0", "P1"]
    time_budget_hours: int = Field(ge=1)
    due_date: date | None = None
    relative_order: Literal["first", "then"] | None = None
    completion_criteria: str
    review_checkpoint: str
    evidence_basis: str


class CareerPreparationPlanOutput(BaseModel):
    """Structured preparation plan with target evidence provenance."""

    target_artifact_id: str
    resolved_target_artifact_id: str
    selected_target_reference: str
    source_url: str
    jd_topics: list[str]
    actions: list[str]
    schedule_assumption: str
    schedule: PreparationSchedule
    plan_items: list[PreparationPlanItem]
    skill_gaps: list[SkillGap] = Field(default_factory=list)


# Preserve the source adapter's public output name for callers that import it directly.
PreparationPlanOutput = CareerPreparationPlanOutput


def build_preparation_plan(
    context: ToolContext, payload: BuildPreparationPlanInput
) -> CareerPreparationPlanOutput:
    """Build actions only for focus terms literally supported by one observed JD."""
    target = resolve_target_evidence(
        context.metadata.get("observed_public_evidence"),
        context.metadata.get("structured_job_candidates"),
        payload.target_artifact_id,
    )
    if target is None:
        raise CareerPlanningError("target_evidence_not_found")
    resolved_artifact_id = target.get("artifact_id")
    if not isinstance(resolved_artifact_id, str) or not resolved_artifact_id.strip():
        raise CareerPlanningError("target_evidence_incomplete")
    source_url = target.get("source_url")
    visible_text = target.get("visible_text")
    if not (
        isinstance(source_url, str)
        and source_url.strip()
        and isinstance(visible_text, str)
        and visible_text.strip()
    ):
        raise CareerPlanningError("target_evidence_incomplete")

    title = target.get("title") if isinstance(target.get("title"), str) else ""
    searchable = f"{title}\n{visible_text}".lower()
    if not _target_matches_goal(context.metadata.get("task_goal"), searchable):
        raise CareerPlanningError("target_role_mismatch")

    topic_pairs = [
        (keyword, keyword.lower())
        for keyword in payload.focus_keywords
        if _focus_keyword_in_text(keyword, searchable)
    ]
    if not topic_pairs:
        raise CareerPlanningError("focus_keywords_not_found")
    topics = [normalized for _display, normalized in topic_pairs]
    # A plan item is an executable commitment, so it must receive at least one
    # hour. Retain all evidence-backed JD topics above, but schedule only the
    # deterministic P0/P1 prefix that the user actually has time to complete.
    scheduled_topic_pairs = topic_pairs[: payload.time_budget_hours]
    topic_text = "、".join(display for display, _normalized in scheduled_topic_pairs)
    actions = _actions_for_topics(topic_text)
    schedule, schedule_assumption = _resolve_schedule(
        payload.target_date, context.metadata.get("confirmed_target_date")
    )
    plan_items = _build_plan_items(scheduled_topic_pairs, payload.time_budget_hours, schedule)

    skill_gaps = (
        _aggregate_skill_gaps(
            context.metadata.get("observed_public_evidence"),
            context.metadata.get("structured_job_candidates"),
            (payload.target_artifact_id, *payload.additional_target_artifact_ids),
            context.metadata.get("confirmed_profile_facts"),
            payload.gap_limit,
        )
        if payload.additional_target_artifact_ids
        else []
    )
    return CareerPreparationPlanOutput(
        target_artifact_id=resolved_artifact_id,
        resolved_target_artifact_id=resolved_artifact_id,
        selected_target_reference=payload.target_artifact_id,
        source_url=source_url,
        jd_topics=topics,
        actions=actions,
        schedule_assumption=schedule_assumption,
        schedule=schedule,
        plan_items=plan_items,
        skill_gaps=skill_gaps,
    )


def _actions_for_topics(topic_text: str) -> list[str]:
    if not topic_text:
        return []
    return [
        f"为 {topic_text} 各准备一个可量化的项目案例，并标明你的具体贡献。",
        f"围绕 JD 中的 {topic_text} 做一次 30 分钟技术讲解演练，准备架构取舍与故障排查追问。",
    ]


def _focus_keyword_in_text(keyword: str, searchable: str) -> bool:
    """Match ASCII identifiers as whole tokens, while preserving CJK substring matching."""
    if re.fullmatch(r"[A-Za-z0-9_]+", keyword):
        return re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(keyword)}(?![A-Za-z0-9_])",
            searchable,
            flags=re.IGNORECASE,
        ) is not None
    return keyword.lower() in searchable


def _resolve_schedule(
    target_date: date | None, confirmed_target_date: object
) -> tuple[PreparationSchedule, str]:
    if target_date is not None:
        if _parse_confirmed_target_date(confirmed_target_date) != target_date:
            raise CareerPlanningError("target_date_unconfirmed")
        return (
            PreparationSchedule(
                kind="target_date",
                target_date=target_date,
                target_date_provenance="user_supplied",
            ),
            "使用用户指定的目标日期。",
        )
    return (
        PreparationSchedule(
            kind="relative",
            relative_window="按 P0 后 P1 的相对顺序完成；具体日期待用户确认。",
        ),
        "未提供目标日期；按相对优先级安排，不生成日历截止日期。",
    )


def _parse_confirmed_target_date(value: object) -> date | None:
    """Parse only trusted context date values; malformed values grant no deadline."""
    if type(value) is date:
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _build_plan_items(
    topic_pairs: list[tuple[str, str]],
    time_budget_hours: int,
    schedule: PreparationSchedule,
) -> list[PreparationPlanItem]:
    if not topic_pairs:
        return []
    base_hours, remaining_hours = divmod(time_budget_hours, len(topic_pairs))
    return [
        PreparationPlanItem(
            topic=normalized,
            priority="P0" if index == 0 else "P1",
            time_budget_hours=base_hours + (1 if index < remaining_hours else 0),
            due_date=schedule.target_date,
            relative_order=("first" if index == 0 else "then")
            if schedule.kind == "relative"
            else None,
            completion_criteria=(
                f"准备一个 {display} 相关项目案例，说明你的具体贡献和可核验结果。"
            ),
            review_checkpoint=(
                f"完成后用 JD 的 {display} 要求复盘：案例是否覆盖职责、取舍和追问。"
            ),
            evidence_basis=f"所选 JD 明确提及 {display}。",
        )
        for index, (display, normalized) in enumerate(topic_pairs)
    ]


def _aggregate_skill_gaps(
    raw_evidence: object,
    structured_candidates: object,
    artifact_ids: tuple[str, ...],
    confirmed_profile_facts: object,
    gap_limit: int,
) -> list[SkillGap]:
    """Return verified skill gaps, counting every canonical JD at most once.

    ``resume_skills`` is model-controlled input and therefore cannot establish
    skill ownership. Only confirmed profile facts may remove a demanded skill
    from the gap list; no confirmed facts means no deterministic gap claim.
    """
    owned = _confirmed_owned_skills(confirmed_profile_facts)
    if owned is None:
        return []
    texts: list[str] = []
    seen_targets: set[tuple[str, str]] = set()
    for artifact_id in artifact_ids:
        item = resolve_target_evidence(raw_evidence, structured_candidates, artifact_id)
        if item is None:
            continue
        identity = _target_identity(item, structured_candidates)
        if identity is not None:
            if identity in seen_targets:
                continue
            seen_targets.add(identity)
        visible_text = item.get("visible_text")
        if isinstance(visible_text, str) and visible_text.strip():
            texts.append(visible_text)
    counts: dict[str, int] = {}
    for text in texts:
        for skill in set(skills_from_text(text)) - owned:
            counts[skill] = counts.get(skill, 0) + 1
    return [
        SkillGap(skill=skill, job_count=count)
        for skill, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[
            :gap_limit
        ]
    ]


def _confirmed_owned_skills(value: object) -> set[str] | None:
    """Return only explicitly confirmed closed-set skills, never inferred facts."""
    if not isinstance(value, dict) or "skills" not in value:
        return None
    skills = value["skills"]
    if not isinstance(skills, list) or not all(isinstance(skill, str) for skill in skills):
        return None
    return {
        normalized
        for skill in skills
        if (normalized := normalize_skill(skill)) is not None
    }


def _target_identity(
    item: dict[str, object], structured_candidates: object
) -> tuple[str, str] | None:
    """Identify a JD without collapsing distinct candidates from one raw page.

    A resolved candidate remains distinct by candidate/canonical identity even
    when its source URL is shared. A complete raw page joins that candidate
    only when the structured evidence proves a one-to-one relationship.
    """
    candidate_identity = _candidate_identity(item)
    if candidate_identity is not None:
        return candidate_identity
    related_candidates = _related_candidate_identities(item, structured_candidates)
    if len(related_candidates) == 1:
        return next(iter(related_candidates))
    return _raw_identity(item)


def _candidate_identity(item: dict[str, object]) -> tuple[str, str] | None:
    """Use candidate identity before page identity for structured JD records."""
    candidate_id = item.get("candidate_id")
    if isinstance(candidate_id, str) and candidate_id.strip():
        return "candidate", candidate_id.strip()
    source_artifact_id = item.get("source_artifact_id")
    artifact_id = item.get("artifact_id")
    if (
        isinstance(source_artifact_id, str)
        and source_artifact_id.strip()
        and isinstance(artifact_id, str)
        and artifact_id.strip()
    ):
        return "candidate_artifact", artifact_id.strip()
    return None


def _related_candidate_identities(
    raw_item: dict[str, object], structured_candidates: object
) -> set[tuple[str, str]]:
    """Find candidates demonstrably derived from a raw artifact or its URL."""
    if not isinstance(structured_candidates, list):
        return set()
    identities: set[tuple[str, str]] = set()
    for candidate in structured_candidates:
        if not isinstance(candidate, dict) or not _candidate_relates_to_raw(candidate, raw_item):
            continue
        identity = _candidate_identity(candidate)
        if identity is not None:
            identities.add(identity)
    return identities


def _candidate_relates_to_raw(candidate: dict[str, object], raw_item: dict[str, object]) -> bool:
    """Match extraction provenance by source artifact first, then source URL."""
    raw_artifact_id = raw_item.get("artifact_id")
    source_artifact_id = candidate.get("source_artifact_id")
    if (
        isinstance(raw_artifact_id, str)
        and raw_artifact_id.strip()
        and isinstance(source_artifact_id, str)
        and source_artifact_id.strip()
        and raw_artifact_id.strip() == source_artifact_id.strip()
    ):
        return True
    raw_source_url = raw_item.get("source_url")
    if not isinstance(raw_source_url, str) or not raw_source_url.strip():
        return False
    normalized_raw_url = _normalized_url(raw_source_url)
    return any(
        isinstance(candidate_url, str)
        and candidate_url.strip()
        and _normalized_url(candidate_url) == normalized_raw_url
        for candidate_url in (candidate.get("page_source_url"), candidate.get("source_url"))
    )


def _raw_identity(item: dict[str, object]) -> tuple[str, str] | None:
    """Identify raw evidence only after candidate-specific handling is exhausted."""
    source_url = item.get("source_url")
    if isinstance(source_url, str) and source_url.strip():
        return "source_url", _normalized_url(source_url)
    content_hash = item.get("content_hash")
    if isinstance(content_hash, str) and content_hash.strip():
        return "content_hash", content_hash.strip()
    artifact_id = item.get("artifact_id")
    if isinstance(artifact_id, str) and artifact_id.strip():
        return "artifact", artifact_id.strip()
    return None


def _normalized_url(value: str) -> str:
    """Normalize harmless URL presentation differences used by alias selectors."""
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value.strip()
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def _target_matches_goal(goal: object, searchable: str) -> bool:
    """Reject a clearly incompatible model-selected JD without role inference."""
    if not isinstance(goal, str) or not goal.strip():
        return True
    lowered_goal = goal.lower()
    if any(marker in lowered_goal for marker in ("应届生", "应届", "校招", "实习生")) and not any(
        marker in searchable
        for marker in (
            "应届生", "应届", "校招", "校园招聘", "毕业生", "campus", "graduate", "实习生", "实习", "intern"
        )
    ):
        return False
    if "ai 应用开发" in lowered_goal or "ai应用开发" in lowered_goal:
        return any(marker in searchable for marker in ("ai", "人工智能", "大模型", "llm")) and any(
            marker in searchable
            for marker in ("应用开发", "应用研发", "应用工程师", "开发工程师", "研发工程师", "后端工程师", "前端工程师", "开发实习", "研发实习", "developer", "engineer")
        )
    if "产品经理" in lowered_goal or "产品类" in lowered_goal or "aigc" in lowered_goal:
        if "产品经理" in lowered_goal and "产品经理" not in searchable:
            return False
        return "aigc" not in lowered_goal or "aigc" in searchable
    if "大模型应用开发" in lowered_goal or "llm 应用" in lowered_goal or "llm应用" in lowered_goal:
        return any(term in searchable for term in ("大模型", "应用开发", "llm", "agent"))
    if "前端开发" in lowered_goal:
        return any(term in searchable for term in ("前端", "frontend"))
    if "java 后端" in lowered_goal or "java后端" in lowered_goal:
        return any(term in searchable for term in ("java", "后端"))
    return True
