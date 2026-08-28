"""Run-level request/result contracts for the deepagents harness.

Field-compatible with ``pi_career_skills.runtime.controller`` so evaluation
records and callers (main.py / main_deepagents.py) stay interchangeable.
Kept in a separate module so ``run_state`` / ``controller`` can import it
without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pi_career_skills.contracts import RunEvent
from pi_career_skills.runtime.budgets import BudgetConsumed, BudgetLimits

#: Default skill scope mirrors pi controller.
ALL_ALLOWED_SKILLS: tuple[str, ...] = (
    "job-discovery",
    "job-matching",
    "resume-tailoring",
    "career-planning",
)


@dataclass
class RunRequest:
    """User request to start a career agent run."""

    task: str
    user_id: str = "eval-user"
    run_id: str | None = None
    allowed_skills: tuple[str, ...] = ALL_ALLOWED_SKILLS
    needed_skills: tuple[str, ...] | None = None
    budget: BudgetLimits | None = None
    seed_artifacts: list[Any] | None = None
    private_context: dict[str, Any] | None = None


@dataclass
class RunResult:
    """Terminal result of a run (field-compatible with pi controller)."""

    run_id: str
    status: str
    summary: str | None
    error_code: str | None
    error_message: str | None
    attempt_count: int
    completed_skills: list[str]
    refs: list[dict[str, str]]
    artifacts: list[dict[str, Any]]
    events: list[RunEvent]
    budget: BudgetConsumed


__all__ = [
    "ALL_ALLOWED_SKILLS",
    "RunRequest",
    "RunResult",
]
