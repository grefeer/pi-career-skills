"""Run-level state machine for the career-skills harness.

The run state tracks lifecycle, terminality, and bookkeeping fields used by
the trusted kernel.  Status transitions are guarded: once a run reaches a
terminal state, further mutation raises ``contract_or_policy_error``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..errors import CareerToolError


class RunStatus(StrEnum):
    """Lifecycle states for one agent run."""

    queued = "queued"
    running = "running"
    waiting_user = "waiting_user"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


_TERMINAL_STATUSES = frozenset(
    {RunStatus.succeeded, RunStatus.failed, RunStatus.cancelled}
)


@dataclass
class RunState:
    """Mutable per-run state owned by the trusted kernel.

    The kernel is the sole writer; agents read projections only and never
    manipulate this object directly.
    """

    run_id: str
    attempt_id: str
    synthetic_user_id: str
    status: RunStatus = RunStatus.running
    summary: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    completed_skills: set[str] = field(default_factory=set)
    stall_streak: int = 0
    terminal: bool = False
    needed_skills: set[str] = field(default_factory=set)
    #: Artifact references the run's summary claims; ``RunCompletionPolicy``
    #: resolves each against ``EvidenceStore`` before declaring success.
    summary_refs: list[dict[str, Any]] | None = None
    chain_context: dict[str, Any] | None = None
    feature_flags: dict[str, Any] = field(default_factory=dict)
    started_at_wall: float = field(default_factory=time.time)


def mark_terminal(state: RunState) -> None:
    """Flip ``state`` to terminal; subsequent transitions will be rejected."""
    state.terminal = True


def transition(
    state: RunState,
    new_status: RunStatus,
    *,
    summary: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """Transition ``state`` to ``new_status`` with optional bookkeeping.

    Raises:
        CareerToolError: with code ``contract_or_policy_error`` if the run is
            already terminal or if the transition is otherwise invalid.
    """
    if state.terminal:
        raise CareerToolError(
            "contract_or_policy_error",
            f"run {state.run_id} is terminal; refusing transition to {new_status.value}",
        )
    state.status = new_status
    if summary is not None:
        state.summary = summary
    if error_code is not None:
        state.error_code = error_code
    if error_message is not None:
        state.error_message = error_message
    if new_status in _TERMINAL_STATUSES:
        mark_terminal(state)


__all__ = [
    "RunStatus",
    "RunState",
    "mark_terminal",
    "transition",
]
