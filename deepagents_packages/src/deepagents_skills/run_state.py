"""Run-level shared state threaded through the deepagents graph.

One ``HarnessState`` is created per ``RunRequest`` and passed into the graph
via ``config["configurable"]["run_state"]`` so the supervisor and every skill
subagent middleware reads the **same** trusted kernel — lifecycle state,
evidence store, budget tracker, tool guard and event log are all reused from
``pi_career_skills`` unchanged (see MIGRATION.md §1.1).

The middleware accesses it through :func:`get_run_state`, which resolves the
object from a ``ToolRuntime``/``Runtime`` config — the same channel the
deepagents task tool uses to propagate the parent configurable into subagents.
"""

from __future__ import annotations

import json
import threading
import uuid
from typing import Any

from pi_career_skills.agents.capabilities import capability_budget_limits
from pi_career_skills.contracts import ToolObservation
from pi_career_skills.runtime.budgets import BudgetLimits, BudgetTracker, ToolCallGuard
from pi_career_skills.runtime.events import EventLogger
from pi_career_skills.runtime.evidence import EvidenceStore
from pi_career_skills.runtime.state import RunState as KernelRunState

from .context_bridge import ContextProjectionBridge
from .contracts import RunRequest

#: Config key under which the HarnessState is threaded via ``configurable``.
CONFIG_KEY = "run_state"

#: Verbatim pi soft-stall wrap-up message (agent_hooks.py:_SOFT_STALL_WRAP_UP).
SOFT_STALL_WRAP_UP = "已连续多轮未产生新证据，请基于现有证据直接给出最终结论并结束。"


class HarnessState:
    """Mutable per-run harness bundle owned by the controller.

    The controller is the only writer of budget/evidence/completion; agents
    (and their middleware) only read projections and feed observations back
    through the evidence boundary.
    """

    def __init__(
        self,
        kernel: KernelRunState,
        *,
        budget: BudgetLimits | None = None,
        private_context: dict[str, Any] | None = None,
        default_task_goal: str | None = None,
    ) -> None:
        self.kernel = kernel
        self.event_log = EventLogger(run_id=kernel.run_id)
        self.store = EvidenceStore()
        self.tracker = BudgetTracker(budget or BudgetLimits())
        self.guard = ToolCallGuard()
        self.context_bridge = ContextProjectionBridge(private_context)
        self.default_task_goal = default_task_goal
        self.delegation_goals: dict[str, str] = {}
        self._tracker_stack: list[tuple[str, Any]] = []

        #: Skills the supervisor may delegate to (set by the controller).
        self.allowed_skills: set[str] = set()

        #: Last hard-halt ``(error_code, message)`` recorded by a middleware.
        self.halt_code: str | None = None
        self.halt_message: str | None = None

        #: Per-attempt soft-stall steering is delivered at most once.
        self.soft_stall_steered = False
        #: External-failure counters mirroring agent_hooks.after_tool_call.
        self.external_failure_counts: dict[str, int] = {}
        #: Per-skill count of delegations that ended WITHOUT a structured
        #: DelegationOutcome (killed by the model-call budget or an error).
        #: Reset per attempt; a repeated kill means the supervisor is
        #: re-delegating into the same dead end and the run must converge to
        #: the store-based completion decision instead of looping to the
        #: wall-clock backstop.
        self.killed_delegation_counts: dict[str, int] = {}

        #: Tool observation channel: a CareerLangchainTool records its
        #: ToolObservation here; EvidenceMiddleware pops it after the handler.
        self._pending: dict[str, ToolObservation] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_request(cls, request: RunRequest) -> HarnessState:
        """Build a fresh harness state for one run request."""
        run_id = request.run_id or uuid.uuid4().hex
        needed = set(request.needed_skills or request.allowed_skills)
        kernel = KernelRunState(
            run_id=run_id,
            attempt_id="",
            synthetic_user_id=request.user_id,
            needed_skills=set(needed),
        )
        state = cls(
            kernel,
            budget=request.budget,
            private_context=request.private_context,
            default_task_goal=request.task,
        )
        state.kernel.attempt_id = uuid.uuid4().hex
        return state

    # ------------------------------------------------------------------
    # Middleware-facing helpers
    # ------------------------------------------------------------------

    def record_observation(self, tool_call_id: str, obs: ToolObservation) -> None:
        """Called by a business tool right after producing its observation."""
        with self._lock:
            self._pending[tool_call_id] = obs

    def take_observation(self, tool_call_id: str) -> ToolObservation | None:
        """Pop the observation recorded for *tool_call_id*, if any."""
        with self._lock:
            return self._pending.pop(tool_call_id, None)

    def halt(self, error_code: str, message: str) -> None:
        """Record a hard halt (first one wins)."""
        if self.halt_code is None:
            self.halt_code = error_code
            self.halt_message = message

    def note_tool_observation_event(
        self,
        tool_name: str,
        obs: ToolObservation,
        promoted_count: int,
    ) -> None:
        """Append a ``tool_observation`` event (mirrors agent_hooks)."""
        self.event_log.append(
            "tool_observation",
            {
                "tool_name": tool_name,
                "status": obs.status,
                "error_code": obs.error_code or "",
                "error_message": getattr(obs, "error_message", "") or "",
                "promoted_artifacts": promoted_count,
            },
        )

    # ------------------------------------------------------------------
    # Projections used by the controller after the graph returns
    # ------------------------------------------------------------------

    def params_hash(self, args: dict[str, Any] | None) -> str:
        """Canonical JSON hash of tool arguments (duplicate/stall key)."""
        return json.dumps(args or {}, ensure_ascii=False, sort_keys=True)

    def snapshot_attempt_id(self) -> str:
        return self.kernel.attempt_id or ""

    def projected_metadata(
        self, skill_name: str, task_goal: str | None = None
    ) -> dict[str, Any]:
        """Return current evidence plus the most specific skill task goal."""
        goal = task_goal or self.delegation_goals.get(skill_name) or self.default_task_goal
        return self.context_bridge.metadata(self.store, task_goal=goal)

    def begin_delegation(self, skill_name: str, task_goal: str) -> None:
        """Install a capability-scoped child tracker for one task call."""
        self.delegation_goals[skill_name] = task_goal
        parent = self.tracker
        self._tracker_stack.append((skill_name, parent))
        self.tracker = parent.child(capability_budget_limits(skill_name))

    def end_delegation(self) -> None:
        """Restore the parent tracker after a task call returns."""
        if not self._tracker_stack:
            return
        _skill_name, parent = self._tracker_stack.pop()
        self.tracker = parent


def get_run_state(runtime: Any) -> HarnessState | None:
    """Resolve the shared HarnessState from a middleware ``ToolRuntime``.

    The deepagents ``task`` tool re-seeds each subagent run from the ambient
    parent config, so the same object is reachable in the supervisor and in
    every skill subagent.
    """
    config = getattr(runtime, "config", None)
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    state = configurable.get(CONFIG_KEY)
    return state if isinstance(state, HarnessState) else None


__all__ = [
    "CONFIG_KEY",
    "SOFT_STALL_WRAP_UP",
    "HarnessState",
    "get_run_state",
]
