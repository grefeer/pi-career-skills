"""Regression tests for the structured delegation contract."""

from __future__ import annotations

import pytest

from pi_career_skills.agents.capabilities import CAPABILITY_REGISTRY
from pi_career_skills.agents.contracts import (
    AgentTask,
    ArtifactRef,
    DelegationAction,
    DelegationOutcome,
    DelegationStatus,
    normalize_agent_task,
)
from pi_career_skills.agents.delegation_tools import make_delegation_tool
from pi_career_skills.agents.prompts import load_archived_skill_prompt
from pi_career_skills.errors import CareerToolError
from pi_career_skills.runtime.budgets import BudgetLimits, BudgetTracker


def test_structured_task_keeps_only_evidence_references() -> None:
    task = normalize_agent_task(
        {
            "objective": "匹配 Java 后端岗位",
            "input_refs": [
                {
                    "artifact_id": "art-1",
                    "source_url": "https://example.com/job/1",
                    "content_hash": "hash-1",
                }
            ],
            "constraints": {"locations": ["北京"]},
            "expected_output": {"artifact_type": "job_matching_report"},
        }
    )

    assert isinstance(task, AgentTask)
    assert task.objective == "匹配 Java 后端岗位"
    assert task.input_refs == (
        ArtifactRef(
            artifact_id="art-1",
            source_url="https://example.com/job/1",
            content_hash="hash-1",
        ),
    )
    assert task.constraints == {"locations": ["北京"]}
    assert task.expected_output is not None
    assert task.expected_output.artifact_type == "job_matching_report"
    assert "visible_text" not in repr(task)


def test_legacy_task_goal_is_normalized_without_breaking_callers() -> None:
    task = normalize_agent_task({"task_goal": "找 Java 岗位"})

    assert task.objective == "找 Java 岗位"
    assert task.input_refs == ()
    assert task.expected_output is None


def test_structured_task_rejects_unknown_fields_and_empty_objective() -> None:
    with pytest.raises(ValueError, match="unknown task fields"):
        normalize_agent_task({"objective": "找岗位", "jd_text": "私有副本"})

    with pytest.raises(ValueError, match="objective"):
        normalize_agent_task({"objective": "  "})


@pytest.mark.parametrize(
    ("raw_status", "status", "action"),
    [
        ("success", DelegationStatus.SUCCESS, DelegationAction.CONTINUE),
        ("partial", DelegationStatus.PARTIAL, DelegationAction.REROUTE),
        ("need_user", DelegationStatus.NEED_USER, DelegationAction.ASK_USER),
        ("retryable", DelegationStatus.RETRYABLE, DelegationAction.RETRY),
        ("blocked", DelegationStatus.BLOCKED, DelegationAction.REROUTE),
        ("failed", DelegationStatus.FAILED, DelegationAction.STOP),
    ],
)
def test_outcome_has_six_states_and_deterministic_action(
    raw_status: str,
    status: DelegationStatus,
    action: DelegationAction,
) -> None:
    outcome = DelegationOutcome(skill="job-matching", status=raw_status)

    assert outcome.status is status
    assert outcome.action is action


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
    assert matching.accepts == {
        "objective",
        "input_refs",
        "constraints",
        "expected_output",
    }
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


@pytest.mark.asyncio
async def test_delegate_tool_accepts_structured_task_and_returns_safe_refs() -> None:
    captured: list[AgentTask] = []

    def runner(task: AgentTask, params: dict[str, object]) -> DelegationOutcome:
        del params
        captured.append(task)
        return DelegationOutcome(
            skill="job-matching",
            status=DelegationStatus.SUCCESS,
            summary="排序完成",
            refs=(ArtifactRef(artifact_id="report-1", content_hash="hash-1"),),
        )

    tool = make_delegation_tool("job-matching", runner)
    result = await tool.execute(
        "call-1",
        {
            "objective": "匹配岗位",
            "input_refs": [{"artifact_id": "jd-1", "content_hash": "jd-hash"}],
            "constraints": {"max_results": 3},
            "expected_output": {"artifact_type": "job_matching_report"},
        },
        None,
        None,
    )

    assert captured[0].objective == "匹配岗位"
    assert captured[0].input_refs[0].artifact_id == "jd-1"
    assert result.details == {
        "skill": "job-matching",
        "status": "success",
        "action": "continue",
        "refs": [{"artifact_id": "report-1", "content_hash": "hash-1"}],
    }
    assert result.terminate is False


def test_child_budget_is_bounded_and_charges_parent() -> None:
    parent = BudgetTracker(BudgetLimits(agent_turns=3, initial_tool_calls=3))
    child = parent.child(BudgetLimits(agent_turns=1, initial_tool_calls=2))
    child.consume_turn()
    child.consume_tool_call()
    assert child.consumed().agent_turns == 1
    assert parent.consumed().agent_turns == 1
    with pytest.raises(CareerToolError):
        child.consume_turn()


def test_archived_skill_prompt_is_injected_with_project_adaptation() -> None:
    prompt = load_archived_skill_prompt("job-matching", "CURATED")
    assert "# Job Matching Skill" in prompt
    assert "Runtime adaptation (pi-career-skills)" in prompt
    assert "NOT WHEN" in prompt
    assert "CURATED" in prompt
