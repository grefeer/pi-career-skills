"""Hook closures shared by supervisor and skill agents.

Extracted from ``controller.py`` to keep the controller under the 800-line
hard cap.  Exposes ``build_controller_hooks(...)`` which returns a small
dataclass with the four agent-loop hooks plus a mutable ``agent_ref_box``
the controller populates before each agent invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pi_ai import TextContent, UserMessage, stream_simple

from ..errors import (
    DUPLICATE_TOOL_CALL,
    NO_PROGRESS,
    WALL_CLOCK_BUDGET_EXHAUSTED,
    CareerToolError,
)
from .budgets import BudgetTracker, ToolCallGuard
from .events import EventLogger
from .evidence import EvidenceStore, canonical_json

#: Soft-stall wrap-up message steered to the agent once per streak.
_SOFT_STALL_WRAP_UP = (
    "已连续多轮未产生新证据，请基于现有证据直接给出最终结论并结束。"
)


@dataclass
class ControllerHooks:
    """Hook bundle returned by ``build_controller_hooks``.

    The four callables are wired into ``AgentOptions``.  ``agent_ref_box``
    is a single-element mutable list the controller populates with the
    active agent right before each loop so soft-stall steering can fire.
    """

    stream_fn: Any
    should_stop_after_turn: Any
    before_tool_call: Any
    after_tool_call: Any
    agent_ref_box: list[Any]  # mutable box: [agent | None]


def build_controller_hooks(
    *,
    tracker: BudgetTracker,
    guard: ToolCallGuard,
    store: EvidenceStore,
    event_log: EventLogger,
    halt_box: list[tuple[str, str] | None],
    skill_name: str | None = None,
) -> ControllerHooks:
    """Build the set of hooks shared by supervisor and skill agents.

    Args:
        tracker: shared budget tracker for the current attempt.
        guard: per-attempt tool-call guard (stall / duplicate tracking).
        store: shared evidence store.
        event_log: run-bound event logger.
        halt_box: mutable single-element list recording ``(code, message)``
            when a hook triggers a hard halt.  Read by the controller after
            each agent loop returns.
        skill_name: human-readable skill/agent kind for event payloads.
            ``None`` means the supervisor.
    """
    soft_stall_steered: list[bool] = [False]
    agent_ref_box: list[Any] = [None]
    kind_label = skill_name or "supervisor"

    def _record_halt(code: str, message: str) -> None:
        if halt_box[0] is None:
            halt_box[0] = (code, message)

    def stream_fn(model: Any, context: Any, options: Any = None) -> Any:
        """Pre-stream hook: charge turn + model request, then delegate."""
        try:
            tracker.consume_turn()
            tracker.consume_model_request(tokens=0)
        except CareerToolError as exc:
            _record_halt(exc.code, exc.message)
            raise

        return stream_simple(model, context, options)

    def should_stop_after_turn(ctx: dict[str, Any]) -> bool:
        """Charge input tokens and check wall-clock exhaustion."""
        message = ctx.get("message")
        usage = getattr(message, "usage", None) if message is not None else None
        if usage is not None:
            input_tokens = (
                getattr(usage, "input_tokens", 0) or 0
            ) + (
                getattr(usage, "cache_read_tokens", 0) or 0
            ) + (
                getattr(usage, "cache_creation_tokens", 0) or 0
            )
            if input_tokens > 0:
                try:
                    tracker.consume_input_tokens(input_tokens)
                except CareerToolError as exc:
                    _record_halt(exc.code, exc.message)
                    return True

        if tracker.wall_clock_exhausted():
            _record_halt(
                WALL_CLOCK_BUDGET_EXHAUSTED, "wall clock budget exhausted"
            )
            return True

        return False

    def before_tool_call(
        ctx: dict[str, Any], cancel_event: Any = None
    ) -> dict[str, Any] | None:
        """Duplicate pre-check + budget admission."""
        del cancel_event
        tool_call = ctx.get("tool_call")
        if tool_call is None:
            return None
        name = getattr(tool_call, "name", "")
        args = ctx.get("args", {}) or {}
        params_hash = canonical_json(args)

        if guard.is_duplicate(name, params_hash):
            return {"block": True, "reason": DUPLICATE_TOOL_CALL}

        try:
            tracker.consume_tool_call(guard.artifact_count)
        except CareerToolError as exc:
            _record_halt(exc.code, exc.message)
            return {"block": True, "reason": exc.code, "terminate": True}

        return None

    def after_tool_call(
        ctx: dict[str, Any], cancel_event: Any = None
    ) -> dict[str, Any] | None:
        """Promote observations, track stall, steer on soft stall."""
        del cancel_event
        tool_call = ctx.get("tool_call")
        if tool_call is None:
            return None
        name = getattr(tool_call, "name", "")
        args = ctx.get("args", {}) or {}
        result = ctx.get("result")
        params_hash = canonical_json(args)

        details = getattr(result, "details", None) if result is not None else None

        # Case 1: skill-agent business tool (details is ToolObservation).
        if _is_tool_observation(details):
            promoted = store.add_observation(details)
            guard.set_artifact_count(len(store.job_bearing_artifacts()))
            succeeded = details.status == "succeeded"
            produced = bool(promoted)

            try:
                signal = guard.note_call(
                    name,
                    params_hash,
                    succeeded=succeeded,
                    produced_artifact=produced,
                )
            except CareerToolError as exc:
                if exc.code == NO_PROGRESS:
                    _record_halt(NO_PROGRESS, exc.message)
                    return {"terminate": True}
                raise

            # Soft stall — steer ONCE with wrap-up message.
            if signal == "soft_stop" and not soft_stall_steered[0]:
                soft_stall_steered[0] = True
                agent = agent_ref_box[0]
                if agent is not None:
                    agent.steer(
                        UserMessage(
                            content=[TextContent(text=_SOFT_STALL_WRAP_UP)],
                            timestamp=0,
                        )
                    )
                    # Only record the warning when steering actually fired —
                    # the event is the test's regression signal for a missing
                    # agent reference (see task J R1 re-review).
                    event_log.append(
                        "stall_soft_warning",
                        {
                            "kind": kind_label,
                            "streak": guard.stall_streak,
                        },
                    )

            event_log.append(
                "tool_observation",
                {
                    "tool_name": name,
                    "status": details.status,
                    "error_code": details.error_code or "",
                    "promoted_artifacts": len(promoted),
                },
            )
            return None

        # Case 2: supervisor delegate tool (details is a dict).
        if (
            isinstance(details, dict)
            and "skill" in details
            and "status" in details
        ):
            skill = details.get("skill", "")
            status = details.get("status", "")
            succeeded = status == "succeeded"
            guard.note_call(
                f"delegate-{skill}",
                params_hash,
                succeeded=succeeded,
                produced_artifact=False,
            )
            event_log.append(
                f"delegation_{status}",
                {
                    "skill": skill,
                    "error_code": details.get("error_code") or "",
                },
            )
            return None

        return None

    return ControllerHooks(
        stream_fn=stream_fn,
        should_stop_after_turn=should_stop_after_turn,
        before_tool_call=before_tool_call,
        after_tool_call=after_tool_call,
        agent_ref_box=agent_ref_box,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_tool_observation(details: Any) -> bool:
    """True when *details* looks like a ToolObservation (tool_name + status)."""
    if details is None:
        return False
    if hasattr(details, "tool_name") and hasattr(details, "status"):
        return True
    return (
        isinstance(details, dict)
        and "tool_name" in details
        and "status" in details
    )


__all__ = [
    "ControllerHooks",
    "build_controller_hooks",
]
