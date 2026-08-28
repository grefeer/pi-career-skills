"""Run-level harness controller for the deepagents career agent.

Mirror of ``pi_career_skills.runtime.controller.CareerRunController`` on the
deepagents + middleware driver (MIGRATION.md §4): the same attempt loop,
budget step-up/restore, wall-clock backstop, outcome decision, matching
fallback, completion gates and auto-recovery — but the supervisor graph is
``create_deep_agent`` and delegation is deepagents' built-in ``task`` tool
with per-skill subagents, instead of pi delegation tools + runners.

The controller remains the trusted kernel: budgets, evidence and completion
are its authority.  Model output is never trusted directly — completed
skills are decided by the per-skill store checkers after the graph returns.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI

from pi_career_skills.context import ToolContext
from pi_career_skills.errors import (
    AUTO_RECOVERY_LIMIT_REACHED,
    MODEL_API_KEY_MISSING,
    WALL_CLOCK_BUDGET_EXHAUSTED,
    CareerToolError,
    redact_message,
)
from pi_career_skills.registry import build_career_tool_registry
from pi_career_skills.runtime.budgets import (
    BudgetConsumed,
    BudgetTracker,
    ToolCallGuard,
)
from pi_career_skills.runtime.completion import (
    RunCompletionPolicy,
    _bounded_summary,
    matching_completed,
    matching_fallback,
)
from pi_career_skills.runtime.context_projection import safe_private_context
from pi_career_skills.runtime.evidence import EvidenceStore
from pi_career_skills.runtime.partial_answer import build_partial_answer
from pi_career_skills.runtime.recovery import AUTO_RECOVERABLE_REASONS, should_auto_recover
from pi_career_skills.runtime.state import RunStatus, transition

from .contracts import RunRequest, RunResult
from .gates import needs_matching_fallback
from .middleware.harness import _SKILL_CHECKERS
from .models import get_deepseek_api_key
from .run_state import CONFIG_KEY, HarnessState
from .skills import build_supervisor_graph


def _default_get_api_key(provider: str) -> str | None:
    """Default API key resolver — checks environment variables."""
    import os

    env_var = {
        "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY",
    }.get(provider)
    if env_var:
        return os.environ.get(env_var)
    return None


class CareerRunController:
    """Deterministic run-level harness driving the deepagents career agent."""

    def __init__(
        self,
        model: BaseChatModel,
        *,
        registry: Any | None = None,
        get_api_key: Callable[[str], str | None] | None = None,
        models: dict[str, BaseChatModel] | None = None,
    ) -> None:
        self._model = model
        self._models = dict(models or {})
        self._registry = (
            registry if registry is not None else build_career_tool_registry()
        )
        self._get_api_key = get_api_key or _default_get_api_key

    def _model_for(self, agent_name: str) -> BaseChatModel:
        """Resolve a capability-specific model, falling back to the run model."""
        return self._models.get(agent_name, self._model)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, request: RunRequest) -> RunResult:
        """Execute one run and return the terminal result."""
        # 1. API key check — fail fast for deepseek without a key.
        if self._is_deepseek_chat(self._model) and not self._model_has_key(
            self._model
        ):
            return self._failed_key_missing(request)
        for routed_model in self._models.values():
            if self._is_deepseek_chat(routed_model) and not self._model_has_key(
                routed_model
            ):
                return self._failed_key_missing(request)

        # 2. Per-run state.
        harness = HarnessState.from_request(request)
        harness.allowed_skills = set(request.allowed_skills)

        # Seed artifacts (chain projection seeding).
        for art in request.seed_artifacts or []:
            seed_artifact(harness.store, art)

        harness.event_log.append(
            "run_started",
            {
                "user_id": request.user_id,
                "allowed_skills": list(request.allowed_skills),
                "needed_skills": sorted(harness.kernel.needed_skills),
            },
        )

        # Run-level consumed accumulator (cumulative across attempts).
        run_accumulator = BudgetConsumed()
        attempt_count = 0
        base_tracker = harness.tracker

        # 3. Attempt loop.
        while True:
            attempt_id = uuid.uuid4().hex
            harness.kernel.attempt_id = attempt_id
            harness.event_log._attempt_id = attempt_id  # noqa: SLF001

            # Step up budget for this attempt.
            stepped_limits = base_tracker.step_up(attempt_count)
            harness.tracker = BudgetTracker(stepped_limits)
            harness.tracker.restore_consumed(run_accumulator, reset_wall_clock=True)

            # Fresh guard + halt state per attempt.
            harness.guard = ToolCallGuard()
            harness.guard.set_artifact_count(len(harness.store.job_bearing_artifacts()))
            harness.halt_code = None
            harness.halt_message = None
            harness.soft_stall_steered = False
            harness.external_failure_counts = {}
            harness.killed_delegation_counts = {}

            attempt_count += 1
            attempt_artifact_count = len(harness.store.job_bearing_artifacts())
            harness.event_log.append(
                "attempt_started",
                {"attempt_index": attempt_count - 1},
            )

            # Build a fresh supervisor graph (fresh attempt_id in ToolContexts).
            graph = build_supervisor_graph(
                self._model,
                self._registry,
                harness,
                allowed_skills=tuple(sorted(harness.allowed_skills)),
                private_context=safe_private_context(request.private_context),
                task_goal=request.task,
                models=self._models,
            )

            harness.tracker.mark_attempt_started()
            try:
                result = await asyncio.wait_for(
                    graph.ainvoke(
                        {"messages": [HumanMessage(content=request.task)]},
                        {"configurable": {CONFIG_KEY: harness}},
                    ),
                    timeout=max(
                        0.1, harness.tracker.remaining_wall_clock_seconds()
                    ),
                )
                outcome_status, outcome_code, outcome_msg = self._decide_outcome(
                    harness=harness, request=request, result=result
                )
            except TimeoutError:
                outcome_status = "waiting_user"
                outcome_code = WALL_CLOCK_BUDGET_EXHAUSTED
                outcome_msg = "run wall-clock budget exhausted"
                # The graph was killed by the wall-clock backstop, but the
                # store is the authority on completion: if the evidence
                # gathered before the kill satisfies the completion gates,
                # succeed on it instead of auto-recovering into attempts that
                # restart from the same already-complete store.  An
                # incomplete store falls through to the recoverable
                # wall-clock path below.
                self._satisfy_from_store(harness, request)
                benign_halt = (
                    harness.halt_code is None
                    or harness.halt_code
                    in {
                        "no_progress",
                        "route_already_consumed",
                        "delegation_retry_limit",
                        "budget_exhausted",
                        WALL_CLOCK_BUDGET_EXHAUSTED,
                    }
                )
                if (
                    benign_halt
                    and harness.kernel.completed_skills.issuperset(
                        request.needed_skills or ()
                    )
                    and harness.store.job_bearing_artifacts()
                ):
                    outcome_status = "succeeded"
                    outcome_code = None
                    outcome_msg = None
            except Exception as exc:  # noqa: BLE001 - catch-all for safety
                outcome_status = "failed"
                outcome_code = "runtime_error"
                outcome_msg = redact_message(str(exc))

            harness.tracker.mark_attempt_finished()

            harness.event_log.append(
                "attempt_finished",
                {
                    "attempt_index": attempt_count - 1,
                    "status": outcome_status,
                    "error_code": outcome_code,
                },
            )

            # Accumulate consumed counters.
            consumed = harness.tracker.consumed()
            run_accumulator = BudgetConsumed(
                agent_turns=consumed.agent_turns,
                tool_calls=consumed.tool_calls,
                model_requests=consumed.model_requests,
                input_tokens=consumed.input_tokens,
                wall_clock_seconds=consumed.wall_clock_seconds,
                auto_recoveries=consumed.auto_recoveries,
            )

            # 4. Decide: succeeded → finalize. auto-recoverable → loop.
            if outcome_status == "succeeded":
                return self._finalize(
                    harness=harness,
                    status="succeeded",
                    error_code=None,
                    error_message=None,
                    attempt_count=attempt_count,
                )

            if (
                outcome_status == "waiting_user"
                and outcome_code is not None
                and should_auto_recover(
                    outcome_code,
                    attempt_count - 1,
                    self._auto_recovery_limit(harness),
                )
            ):
                attempt_added_artifacts = (
                    len(harness.store.job_bearing_artifacts())
                    - attempt_artifact_count
                )
                if attempt_added_artifacts <= 0 and outcome_code == "no_progress":
                    if harness.store.job_bearing_artifacts() and not harness.kernel.summary:
                        harness.kernel.summary = (
                            f"已保留 {len(harness.store.job_bearing_artifacts())} 条持久化证据；"
                            f"本轮因 {outcome_code} 未继续重复尝试。"
                        )
                    return self._finalize(
                        harness=harness,
                        status="waiting_user",
                        error_code=outcome_code,
                        error_message=outcome_msg,
                        attempt_count=attempt_count,
                    )
                try:
                    harness.tracker.record_recovery()
                    run_accumulator.auto_recoveries = (
                        harness.tracker.consumed().auto_recoveries
                    )
                except CareerToolError:
                    return self._finalize(
                        harness=harness,
                        status="waiting_user",
                        error_code=AUTO_RECOVERY_LIMIT_REACHED,
                        error_message=outcome_msg,
                        attempt_count=attempt_count,
                    )
                continue

            # Not recoverable — finalize.
            final_code = outcome_code
            if (
                outcome_status == "waiting_user"
                and outcome_code in AUTO_RECOVERABLE_REASONS
                and attempt_count - 1 >= self._auto_recovery_limit(harness)
            ):
                final_code = AUTO_RECOVERY_LIMIT_REACHED

            return self._finalize(
                harness=harness,
                status=outcome_status,
                error_code=final_code,
                error_message=outcome_msg,
                attempt_count=attempt_count,
            )

    # ------------------------------------------------------------------
    # Fast-fail helpers
    # ------------------------------------------------------------------

    def _is_deepseek_chat(self, model: BaseChatModel) -> bool:
        return isinstance(model, ChatOpenAI) and "deepseek" in str(
            getattr(model, "base_url", "") or ""
        )

    def _model_has_key(self, model: BaseChatModel) -> bool:
        key = getattr(model, "openai_api_key", None) or getattr(
            model, "api_key", None
        )
        return bool(key or get_deepseek_api_key())

    def _auto_recovery_limit(self, harness: HarnessState) -> int:
        return harness.tracker.limits.auto_recoveries

    def _failed_key_missing(self, request: RunRequest) -> RunResult:
        """Return a failed result with zero attempts — no model loop."""
        return RunResult(
            run_id=request.run_id or uuid.uuid4().hex,
            status="failed",
            summary=None,
            error_code=MODEL_API_KEY_MISSING,
            error_message="API key missing for provider deepseek",
            attempt_count=0,
            completed_skills=[],
            refs=[],
            artifacts=[],
            events=[],
            budget=BudgetConsumed(),
        )

    # ------------------------------------------------------------------
    # Outcome decision
    # ------------------------------------------------------------------

    def _decide_outcome(
        self, *, harness: HarnessState, request: RunRequest, result: Any
    ) -> tuple[str, str | None, str | None]:
        """Determine the attempt outcome after the supervisor graph returns."""
        state = harness.kernel
        store = harness.store

        # Authoritative completion from the store (model output untrusted).
        for skill, checker in _SKILL_CHECKERS.items():
            if skill in harness.allowed_skills and checker(store):
                state.completed_skills.add(skill)

        # A hard halt recorded by middleware.
        if harness.halt_code is not None:
            halt_code = harness.halt_code
            # Escalate to an anti-bot hand-off when a public source already
            # returned an anti-bot/manual-review error (pi controller §4).
            if (
                halt_code
                in {
                    "auto_recovery_limit_reached",
                    "no_progress",
                    "route_already_consumed",
                    "budget_exhausted",
                    WALL_CLOCK_BUDGET_EXHAUSTED,
                }
                and any(
                    (getattr(event, "payload", {}) or {}).get("error_code")
                    in {
                        "anti_bot_challenge",
                        "captcha",
                        "login_required",
                        "needs_manual_review",
                    }
                    for event in harness.event_log.events()
                )
            ):
                return (
                    "waiting_user",
                    "anti_bot_challenge",
                    "public source requires manual review after an observed anti-bot challenge",
                )
            if (
                halt_code
                in {
                    "no_progress",
                    "route_already_consumed",
                    "delegation_retry_limit",
                    "budget_exhausted",
                    WALL_CLOCK_BUDGET_EXHAUSTED,
                }
                and state.completed_skills.issuperset(request.needed_skills or ())
                and store.job_bearing_artifacts()
            ):
                return ("succeeded", None, None)
            return ("waiting_user", halt_code, harness.halt_message)

        # Matching fallback — deterministic one-shot.
        if needs_matching_fallback(state, store, request.needed_skills):
            # Mirrors pi's fallback projection (controller.py §6.6): the
            # candidates must come from the *store* via the projection bridge,
            # not from a bare copy of private_context — safe_private_context
            # lacks the projected ``structured_job_candidates`` pool, so the
            # fallback would run blind when the subagent never called the tool.
            kernel_ctx = ToolContext(
                user_id=state.synthetic_user_id,
                run_id=state.run_id,
                attempt_id=state.attempt_id,
                skill_name=None,
                metadata=harness.projected_metadata("job-matching"),
            )
            with contextlib.suppress(CareerToolError):
                matching_fallback(
                    self._registry, kernel_ctx, store, harness.tracker, harness.guard
                )
            if matching_completed(store):
                state.completed_skills.add("job-matching")

        # Extract summary.
        summary = last_assistant_text(result) or state.summary
        summary = _bounded_summary(summary)

        state.summary = summary
        state.summary_refs = store.refs()

        # Run completion policy.
        status, code = RunCompletionPolicy().evaluate(state, store, summary)
        if (
            status == "waiting_user"
            and code == "completion_evidence_unavailable"
            and state.completed_skills.issuperset(request.needed_skills or ())
            and store.job_bearing_artifacts()
        ):
            state.summary = summary or "已根据持久化证据完成请求。"
            state.summary_refs = store.refs()
            return ("succeeded", None, None)
        return (status, code, None)

    def _satisfy_from_store(
        self, harness: HarnessState, request: RunRequest
    ) -> None:
        """Mark completed skills from the store — authoritative, model output
        untrusted — plus the deterministic matching fallback.

        Shared by the wall-clock timeout path so a graph that was killed by
        the backstop can still succeed on the evidence it gathered.  Mirrors
        ``_decide_outcome``'s completion loop + matching fallback without
        touching the halt/anti-bot escalation logic.
        """
        state = harness.kernel
        store = harness.store
        for skill, checker in _SKILL_CHECKERS.items():
            if skill in harness.allowed_skills and checker(store):
                state.completed_skills.add(skill)
        if needs_matching_fallback(state, store, request.needed_skills):
            kernel_ctx = ToolContext(
                user_id=state.synthetic_user_id,
                run_id=state.run_id,
                attempt_id=state.attempt_id,
                skill_name=None,
                metadata=harness.projected_metadata("job-matching"),
            )
            with contextlib.suppress(CareerToolError):
                matching_fallback(
                    self._registry, kernel_ctx, store, harness.tracker, harness.guard
                )
            if matching_completed(store):
                state.completed_skills.add("job-matching")

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    def _finalize(
        self,
        *,
        harness: HarnessState,
        status: str,
        error_code: str | None,
        error_message: str | None,
        attempt_count: int,
    ) -> RunResult:
        """Transition state to terminal, append final event, return result."""
        # Non-succeeded runs still owe the user a text answer from the partial
        # evidence (budget/wall-clock/evidence-gate terminations).  The partial
        # answer is deterministic and source-backed — it never fabricates jobs.
        if status in {"waiting_user", "failed"}:
            partial = build_partial_answer(harness.store, error_code=error_code)
            if partial:
                harness.kernel.summary = partial
        transition(
            harness.kernel,
            RunStatus(status),
            summary=harness.kernel.summary,
            error_code=error_code,
            error_message=error_message,
        )

        harness.event_log.append(
            "run_finalized",
            {
                "status": status,
                "error_code": error_code or "",
                "attempt_count": attempt_count,
                "completed_skills": sorted(harness.kernel.completed_skills),
            },
        )

        return RunResult(
            run_id=harness.kernel.run_id,
            status=status,
            summary=harness.kernel.summary,
            error_code=error_code,
            error_message=error_message,
            attempt_count=attempt_count,
            completed_skills=sorted(harness.kernel.completed_skills),
            refs=harness.store.refs(),
            artifacts=serialize_artifacts(harness.store),
            events=harness.event_log.events(),
            budget=harness.tracker.consumed(),
        )


# ======================================================================
# Module-level helpers
# ======================================================================


def seed_artifact(store: EvidenceStore, artifact: Any) -> None:
    """Seed a chain artifact through the runtime evidence boundary."""
    from pi_career_skills.runtime.context_projection import seed_artifact as _seed

    _seed(store, artifact)


def last_assistant_text(result: Any) -> str | None:
    """Extract text from the last AI message in the graph result state.

    The deepagents supervisor state's ``messages`` channel holds the
    supervisor's own turns; the subagents' messages live in their private
    subgraph state and never leak here.
    """
    messages = (result or {}).get("messages") or []
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            if isinstance(message.content, str) and message.content.strip():
                return message.content
            parts: list[str] = []
            for block in message.content if isinstance(message.content, list) else []:
                if isinstance(block, dict) and block.get("text"):
                    parts.append(str(block["text"]))
            if parts:
                return "".join(parts)
    return None


def serialize_artifacts(store: EvidenceStore) -> list[dict[str, Any]]:
    """Serialize all evidence-store artifacts into plain dicts (with content)."""
    serialized: list[dict[str, Any]] = []
    for art in store.job_bearing_artifacts():
        serialized.append(
            {
                "artifact_id": art.artifact_id,
                "artifact_type": art.artifact_type,
                "source_url": art.source_url,
                "content_hash": art.content_hash,
                "quality": art.quality or None,
                "content_json": dict(art.content) if art.content else {},
            }
        )
    return serialized


__all__ = ["CareerRunController"]
