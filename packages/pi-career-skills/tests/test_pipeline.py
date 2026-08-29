"""Deterministic pipeline-loop tests — skill order, forward-only routing,
and the one-retry SkillRetryableError behaviour (replaces the LangGraph shell)."""

from __future__ import annotations

from typing import Any

import pytest

from pi_career_skills.errors import SkillRetryableError
from pi_career_skills.runtime.controller import SKILL_ORDER, run_pipeline


async def _succeed_node(skill: str, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "completed": set(state["completed"]) | {skill},
        "status": "succeeded",
        "code": None,
        "message": f"{skill} done",
    }


async def _run(
    needed: set[str],
    completed: set[str] | None = None,
    run_node: Any = None,
) -> tuple[list[str], dict[str, Any]]:
    calls: list[str] = []

    async def default_node(skill: str, state: dict[str, Any]) -> dict[str, Any]:
        calls.append(skill)
        return await _succeed_node(skill, state)

    node = run_node or default_node
    out = await run_pipeline(
        node, task="t", needed=set(needed), completed=set(completed or set())
    )
    return calls, out


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


async def test_pipeline_starts_with_job_discovery_and_runs_needed_in_order() -> None:
    calls, out = await _run({"job-discovery", "job-matching", "resume-tailoring"})
    assert calls == ["job-discovery", "job-matching", "resume-tailoring"]
    assert out["completed"] == {"job-discovery", "job-matching", "resume-tailoring"}
    assert out["task"] == "t"  # task/needed carried forward for the router


async def test_pipeline_skips_unneeded_skills_but_keeps_first_node() -> None:
    """job-discovery is the unconditional first node; only needed skills run."""
    calls, out = await _run({"resume-tailoring"})
    assert calls == ["job-discovery", "resume-tailoring"]
    assert out["completed"] == {"job-discovery", "resume-tailoring"}


async def test_pipeline_stops_on_non_continue_status() -> None:
    """A node returning blocked/failed stops the pipeline at that node."""
    calls: list[str] = []

    async def run_node(skill: str, state: dict[str, Any]) -> dict[str, Any]:
        calls.append(skill)
        if skill == "job-discovery":
            return {
                "completed": set(state["completed"]),
                "status": "blocked",
                "code": "delegation_skill_not_allowed",
                "message": "blocked",
            }
        return await _succeed_node(skill, state)

    _, out = await _run({"job-discovery", "job-matching"}, run_node=run_node)
    assert calls == ["job-discovery"]
    assert out["status"] == "blocked"
    assert out["completed"] == set()


async def test_pipeline_never_reruns_a_completed_skill() -> None:
    """Completed skills are skipped on later nodes (forward-only routing)."""
    calls, out = await _run(
        {"job-matching", "job-discovery"}, completed={"job-discovery"}
    )
    assert calls == ["job-discovery", "job-matching"]
    assert out["completed"] == {"job-discovery", "job-matching"}


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


async def test_retryable_node_is_retried_then_propagates() -> None:
    """A node raising SkillRetryableError is retried once and the exception
    propagates once the node's retry budget is exhausted."""
    attempts: list[str] = []

    async def run_node(skill: str, state: dict[str, Any]) -> dict[str, Any]:
        del state
        attempts.append(skill)
        raise SkillRetryableError("wall_clock_budget_exhausted", "boom")

    with pytest.raises(SkillRetryableError) as exc_info:
        await _run({"job-discovery"}, run_node=run_node)
    assert exc_info.value.code == "wall_clock_budget_exhausted"
    assert attempts == ["job-discovery", "job-discovery"]


async def test_retryable_node_recovers_on_retry() -> None:
    """A transient node failure succeeds on the retry and the pipeline
    continues normally."""
    attempts: list[str] = []

    async def run_node(skill: str, state: dict[str, Any]) -> dict[str, Any]:
        attempts.append(skill)
        if len(attempts) == 1:
            raise SkillRetryableError("wall_clock_budget_exhausted", "boom")
        return await _succeed_node(skill, state)

    calls, out = await _run({"job-matching", "job-discovery"}, run_node=run_node)
    assert attempts == ["job-discovery", "job-discovery", "job-matching"]
    assert out["status"] == "succeeded"
    assert out["completed"] == {"job-discovery", "job-matching"}


async def test_non_retryable_exception_is_not_retried() -> None:
    """Only SkillRetryableError is retried, not arbitrary errors."""
    attempts: list[str] = []

    async def run_node(skill: str, state: dict[str, Any]) -> dict[str, Any]:
        del state
        attempts.append(skill)
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        await _run({"job-discovery"}, run_node=run_node)
    assert attempts == ["job-discovery"]


def test_skill_order_matches_capability_prerequisite_chain() -> None:
    assert SKILL_ORDER == (
        "job-discovery",
        "job-matching",
        "resume-tailoring",
        "career-planning",
    )
