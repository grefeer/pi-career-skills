"""Skill-gate predicates shared by the middleware gate and the controller.

These mirror ``pi_career_skills.runtime.controller``'s per-skill delegation
validation (MIGRATION.md §6.7): each skill's evidence prerequisites, the
resume-tailoring no-match shortcut, and the role-plan exemption.  Keeping them
in one module guarantees the supervisor-side gate and the controller's
outcome decision agree.
"""

from __future__ import annotations

from typing import Any

from pi_career_skills.runtime.evidence import EvidenceStore


def skill_has_evidence(skill: str, store: EvidenceStore) -> bool:
    """Check whether *skill*'s prerequisite evidence exists."""
    if skill == "job-discovery":
        return True  # no prerequisite
    if skill == "job-matching":
        # Matching can score either structured candidates or a complete public
        # JD directly (``match_observed_jobs`` has a raw-evidence fallback).
        for art in store.job_bearing_artifacts():
            if art.artifact_type == "structured_job_details":
                return True
            if (
                art.artifact_type == "public_job_page"
                and art.quality == "jd_complete"
                and isinstance(art.content, dict)
                and isinstance(art.content.get("visible_text"), str)
                and bool(art.content["visible_text"].strip())
            ):
                return True
        return False
    if skill in {"resume-tailoring", "career-planning"}:
        return bool(store.job_bearing_artifacts())
    return True


def matching_explicit_no_match(store: EvidenceStore) -> bool:
    """Whether a persisted matching report proves no eligible target exists."""
    for artifact in store.job_bearing_artifacts():
        if artifact.artifact_type != "job_matching_report":
            continue
        content = artifact.content if isinstance(artifact.content, dict) else {}
        count = content.get("evaluated_candidate_count")
        if (
            content.get("no_match_reason") == "no_candidate_satisfied_constraints"
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 0
        ):
            return True
    return False


def role_plan_allowed(task_goal: Any) -> bool:
    """Allow an explicit role-level plan when no employer JD is available."""
    if not isinstance(task_goal, str) or not task_goal.strip():
        return False
    lowered = task_goal.lower()
    return any(
        marker in lowered
        for marker in ("岗位准备计划", "面试准备计划", "求职准备计划", "职业准备计划")
    )


def needs_matching_fallback(
    state: Any, store: EvidenceStore, needed_skills: Any
) -> bool:
    """True when matching is needed but hasn't been produced yet, and
    structured candidates exist to match against."""
    from pi_career_skills.runtime.completion import matching_completed

    needed = set(needed_skills or ())
    if "job-matching" not in needed:
        return False
    if "job-matching" in state.completed_skills:
        return False
    if matching_completed(store):
        return False
    for art in store.job_bearing_artifacts():
        if art.artifact_type == "structured_job_details":
            return True
    return False


__all__ = [
    "matching_explicit_no_match",
    "needs_matching_fallback",
    "role_plan_allowed",
    "skill_has_evidence",
]
