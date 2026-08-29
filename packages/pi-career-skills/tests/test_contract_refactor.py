"""Regression tests for the structured delegation contract."""

from __future__ import annotations

import pytest

from pi_career_skills.agents.capabilities import CAPABILITY_REGISTRY
from pi_career_skills.agents.contracts import (
    AgentTask,
    ArtifactRef,
    DelegationOutcome,
    DelegationStatus,
)
from pi_career_skills.agents.prompts import load_archived_skill_prompt
from pi_career_skills.errors import CareerToolError
from pi_career_skills.runtime.budgets import BudgetLimits, BudgetTracker, ToolCallGuard


def test_agent_task_is_a_bare_stripped_objective() -> None:
    task = AgentTask(objective="  找 Java 岗位  ")
    assert task.objective == "找 Java 岗位"
    assert task.to_dict() == {"objective": "找 Java 岗位"}
    with pytest.raises(ValueError, match="objective"):
        AgentTask(objective="   ")


def test_legacy_outcome_statuses_are_adapted_at_boundary() -> None:
    assert DelegationOutcome(skill="job-discovery", status="succeeded").status is DelegationStatus.SUCCESS
    assert DelegationOutcome(skill="job-discovery", status="error").status is DelegationStatus.FAILED


def test_capability_registry_describes_all_skills_and_side_effects() -> None:
    assert set(CAPABILITY_REGISTRY) == {
        "job-discovery",
        "job-matching",
        "resume-tailoring",
        "career-planning",
    }
    matching = CAPABILITY_REGISTRY.get("job-matching")
    assert matching.returns == {"job_matching_report"}
    assert matching.side_effects == {"write_run_artifact"}
    assert "match-observed-jobs" in matching.tool_names
    assert matching.prerequisite == "structured_job_details"


def test_career_planning_completion_requires_a_persisted_plan() -> None:
    from pi_career_skills.runtime.completion import career_planning_completed

    class Store:
        def __init__(self, artifacts: list[object]) -> None:
            self._artifacts = artifacts

        def job_bearing_artifacts(self) -> list[object]:
            return self._artifacts

    class Artifact:
        artifact_type = "career_preparation_plan"
        content = {"target_artifact_id": "jd-1", "action_items": [{"topic": "Java"}]}

    assert career_planning_completed(Store([Artifact()]))
    assert not career_planning_completed(Store([]))


def test_career_planning_is_a_default_capability_with_evidence_prerequisite() -> None:
    from pi_career_skills.runtime.controller import RunRequest, _skill_has_evidence

    assert "career-planning" in RunRequest(task="准备求职").allowed_skills

    class Store:
        def job_bearing_artifacts(self) -> list[object]:
            return []

    assert not _skill_has_evidence("career-planning", Store())


def test_matching_accepts_complete_public_page_without_structured_projection() -> None:
    """Raw JD fallback is a valid matching prerequisite for static pages."""
    from pi_career_skills.runtime.controller import _skill_has_evidence

    class Artifact:
        artifact_type = "public_job_page"
        quality = "jd_complete"
        content = {"visible_text": "AI Agent engineer: Python and RAG."}

    class Store:
        def job_bearing_artifacts(self) -> list[object]:
            return [Artifact()]

    assert _skill_has_evidence("job-matching", Store())


def test_outcome_safe_refs_project_only_evidence_handles() -> None:
    """The delegation boundary projects only safe evidence refs outward."""
    outcome = DelegationOutcome(
        skill="job-matching",
        status=DelegationStatus.SUCCESS,
        summary="排序完成",
        refs=(
            ArtifactRef(artifact_id="report-1", content_hash="hash-1"),
            {"artifact_id": "jd-1", "source_url": "https://example.com/job/1"},
        ),
    )
    assert outcome.safe_refs() == [
        {"artifact_id": "report-1", "content_hash": "hash-1"},
        {"artifact_id": "jd-1", "source_url": "https://example.com/job/1"},
    ]


def test_child_budget_is_bounded_and_charges_parent() -> None:
    parent = BudgetTracker(BudgetLimits(agent_turns=3, initial_tool_calls=3))
    child = parent.child(BudgetLimits(agent_turns=1, initial_tool_calls=2))
    child.consume_turn()
    child.consume_tool_call()
    assert child.consumed().agent_turns == 1
    assert parent.consumed().agent_turns == 1
    with pytest.raises(CareerToolError):
        child.consume_turn()


def test_repeated_failed_tool_signal_stops_retry_loop() -> None:
    guard = ToolCallGuard()
    assert guard.note_call("tool", "same", succeeded=False, produced_artifact=False) is None
    assert guard.note_call("tool", "same", succeeded=False, produced_artifact=False) is None
    assert guard.note_call("tool", "same", succeeded=False, produced_artifact=False) == "repeated_tool_failure"


def test_archived_skill_prompt_is_injected_with_project_adaptation() -> None:
    prompt = load_archived_skill_prompt("job-matching", "CURATED")
    assert "# Job Matching Skill" in prompt
    assert "Runtime adaptation (pi-career-skills)" in prompt
    assert "NOT WHEN" in prompt
    assert "CURATED" in prompt
