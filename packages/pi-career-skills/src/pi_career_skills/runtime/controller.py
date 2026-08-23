"""Run-level harness controller — drives real pi-agent loops with budgets,
evidence promotion, stall detection, auto-recovery, and completion gates.

Implements migration plan §4.1 / §4.2 (controller duties + lifecycle), §6.5
(per-skill completion checkers), §6.6 (matching fallback), §6.7 (delegation),
and §7 (budgets / dynamic tool quota / stall / auto-recovery).

The controller is the trusted kernel: budgets, evidence, and completion are
*its* authority — model output is never trusted directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pi_agent_core import Agent
from pi_ai import AssistantMessage, Model, ToolResultMessage

from ..agents.delegation_tools import DelegationOutcome, DelegationRunner
from ..agents.factory import build_skill_agent, build_supervisor_agent
from ..context import ToolContext
from ..contracts import RunEvent
from ..errors import (
    AUTO_RECOVERY_LIMIT_REACHED,
    DELEGATION_SKILL_ALREADY_SUCCEEDED,
    DELEGATION_SKILL_NOT_ALLOWED,
    INVALID_MODEL_RESPONSE,
    MODEL_API_KEY_MISSING,
    TARGET_EVIDENCE_NOT_FOUND,
    CareerToolError,
    redact_message,
)
from ..registry import build_career_tool_registry
from .agent_hooks import ControllerHooks, build_controller_hooks
from .budgets import BudgetConsumed, BudgetLimits, BudgetTracker, ToolCallGuard
from .completion import (
    RunCompletionPolicy,
    _bounded_summary,
    discovery_completed,
    matching_completed,
    matching_fallback,
    tailoring_completed,
)
from .events import EventLogger
from .evidence import EvidenceStore
from .recovery import AUTO_RECOVERABLE_REASONS, should_auto_recover
from .state import RunState, RunStatus, transition

#: Per-skill completion checker (§6.5).
_SKILL_CHECKERS: dict[str, Callable[[Any], bool]] = {
    "job-discovery": discovery_completed,
    "job-matching": matching_completed,
    "resume-tailoring": tailoring_completed,
}


@dataclass
class RunRequest:
    """User request to start a career agent run."""

    task: str
    user_id: str = "eval-user"
    run_id: str | None = None
    allowed_skills: tuple[str, ...] = (
        "job-discovery",
        "job-matching",
        "resume-tailoring",
    )
    needed_skills: tuple[str, ...] | None = None
    budget: BudgetLimits | None = None
    seed_artifacts: list[Any] | None = None


@dataclass
class RunResult:
    """Terminal result of a run."""

    run_id: str
    status: str
    summary: str | None
    error_code: str | None
    error_message: str | None
    attempt_count: int
    completed_skills: list[str]
    refs: list[dict[str, str]]
    events: list[RunEvent]
    budget: BudgetConsumed


class CareerRunController:
    """Deterministic run-level harness for the career skills platform.

    Drives real pi-agent supervisor + skill subagent loops with the shared
    trusted kernel: budgets, evidence promotion, duplicate detection, stall
    handling, auto-recovery, and completion gates.

    The controller NEVER trusts model output directly.  Observations,
    artifacts, and budgets are the authority.
    """

    def __init__(
        self,
        model: Model,
        *,
        registry: Any | None = None,
        get_api_key: Callable[[str], str | None] | None = None,
    ) -> None:
        self._model = model
        self._registry = (
            registry if registry is not None else build_career_tool_registry()
        )
        self._get_api_key = get_api_key or _default_get_api_key

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, request: RunRequest) -> RunResult:
        """Execute one run and return the terminal result."""
        # 1. API key check — fail fast for deepseek without a key.
        api_key = self._get_api_key(self._model.provider)
        if self._model.provider == "deepseek" and not api_key:
            return self._failed_key_missing(request)

        # 2. Per-run state.
        run_id = request.run_id or uuid.uuid4().hex
        allowed_skills = request.allowed_skills
        needed_skills = set(request.needed_skills or request.allowed_skills)
        budget_limits = request.budget or BudgetLimits()

        state = RunState(
            run_id=run_id,
            attempt_id="",
            synthetic_user_id=request.user_id,
            needed_skills=set(needed_skills),
        )
        event_log = EventLogger(run_id=run_id)
        store = EvidenceStore()

        # Seed artifacts (Phase 8 chain projection seeding).
        if request.seed_artifacts:
            for art in request.seed_artifacts:
                self._seed_artifact(store, art)

        event_log.append(
            "run_started",
            {
                "user_id": request.user_id,
                "allowed_skills": list(allowed_skills),
                "needed_skills": list(needed_skills),
            },
        )

        # Run-level consumed accumulator (cumulative across attempts).
        run_accumulator = BudgetConsumed()
        attempt_count = 0
        base_tracker = BudgetTracker(budget_limits)

        # 3. Attempt loop.
        while True:
            attempt_id = uuid.uuid4().hex
            state.attempt_id = attempt_id
            event_log._attempt_id = attempt_id  # noqa: SLF001 — internal field

            # Step up budget for this attempt.
            stepped_limits = base_tracker.step_up(attempt_count)
            tracker = BudgetTracker(stepped_limits)
            tracker.restore_consumed(run_accumulator, reset_wall_clock=True)

            guard = ToolCallGuard()
            guard.set_artifact_count(len(store.job_bearing_artifacts()))

            # Per-attempt halt tracking.
            halt_box: list[tuple[str, str] | None] = [None]

            attempt_count += 1
            event_log.append(
                "attempt_started",
                {"attempt_index": attempt_count - 1},
            )

            # Build hooks for this attempt (supervisor-level; skill agents
            # share the same halt_box / tracker / guard / store).
            hooks = build_controller_hooks(
                tracker=tracker,
                guard=guard,
                store=store,
                event_log=event_log,
                halt_box=halt_box,
            )

            # Build per-skill runners for delegation.
            runners: dict[str, DelegationRunner] = {}
            for skill in allowed_skills:
                runners[skill] = self._make_runner_for_skill(
                    skill=skill,
                    state=state,
                    store=store,
                    tracker=tracker,
                    guard=guard,
                    event_log=event_log,
                    hooks=hooks,
                    allowed_skills=allowed_skills,
                    attempt_id=attempt_id,
                    halt_box=halt_box,
                )

            # Build fresh supervisor via the factory.
            supervisor = build_supervisor_agent(
                self._model,
                runners,
                allowed_skills=allowed_skills,
                registry=self._registry,
                stream_fn=hooks.stream_fn,
                get_api_key=self._get_api_key,
                before_tool_call=hooks.before_tool_call,
                after_tool_call=hooks.after_tool_call,
                should_stop_after_turn=hooks.should_stop_after_turn,
            )

            tracker.mark_attempt_started()
            try:
                hooks.agent_ref_box[0] = supervisor
                await supervisor.prompt(request.task)

                # Continue loop while last message is a tool result and no halt.
                # Always let the supervisor produce a final text turn after the
                # last tool call, even if all needed skills are already satisfied
                # (so the completion policy gets a summary).
                while (
                    halt_box[0] is None
                    and supervisor.state.messages
                    and isinstance(
                        supervisor.state.messages[-1], ToolResultMessage
                    )
                ):
                    hooks.agent_ref_box[0] = supervisor
                    await supervisor.continue_()

                outcome_status, outcome_code, outcome_msg = self._decide_outcome(
                    state=state,
                    store=store,
                    tracker=tracker,
                    guard=guard,
                    supervisor=supervisor,
                    request=request,
                    event_log=event_log,
                    attempt_id=attempt_id,
                    halt_box=halt_box,
                )
            except Exception as exc:  # noqa: BLE001 - catch-all for safety
                outcome_status = "failed"
                outcome_code = "runtime_error"
                outcome_msg = redact_message(str(exc))

            tracker.mark_attempt_finished()

            event_log.append(
                "attempt_finished",
                {
                    "attempt_index": attempt_count - 1,
                    "status": outcome_status,
                    "error_code": outcome_code,
                },
            )

            # Accumulate consumed counters.
            consumed = tracker.consumed()
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
                    state=state,
                    store=store,
                    tracker=tracker,
                    event_log=event_log,
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
                    budget_limits.auto_recoveries,
                )
            ):
                try:
                    tracker.record_recovery()
                    run_accumulator.auto_recoveries = (
                        tracker.consumed().auto_recoveries
                    )
                except CareerToolError:
                    # At limit — finalize as waiting_user with limit-reached code.
                    return self._finalize(
                        state=state,
                        store=store,
                        tracker=tracker,
                        event_log=event_log,
                        status="waiting_user",
                        error_code=AUTO_RECOVERY_LIMIT_REACHED,
                        error_message=outcome_msg,
                        attempt_count=attempt_count,
                    )
                continue

            # Not recoverable — finalize.
            # If the reason is recoverable but we hit the attempt limit,
            # surface auto_recovery_limit_reached instead of the raw error.
            final_code = outcome_code
            if (
                outcome_status == "waiting_user"
                and outcome_code in AUTO_RECOVERABLE_REASONS
                and attempt_count - 1 >= budget_limits.auto_recoveries
            ):
                final_code = AUTO_RECOVERY_LIMIT_REACHED

            return self._finalize(
                state=state,
                store=store,
                tracker=tracker,
                event_log=event_log,
                status=outcome_status,
                error_code=final_code,
                error_message=outcome_msg,
                attempt_count=attempt_count,
            )

    # ------------------------------------------------------------------
    # Fast-fail helpers
    # ------------------------------------------------------------------

    def _failed_key_missing(self, request: RunRequest) -> RunResult:
        """Return a failed result with zero attempts — no model loop."""
        run_id = request.run_id or uuid.uuid4().hex
        return RunResult(
            run_id=run_id,
            status="failed",
            summary=None,
            error_code=MODEL_API_KEY_MISSING,
            error_message="API key missing for provider deepseek",
            attempt_count=0,
            completed_skills=[],
            refs=[],
            events=[],
            budget=BudgetConsumed(),
        )

    # ------------------------------------------------------------------
    # Seed artifacts
    # ------------------------------------------------------------------

    def _seed_artifact(self, store: EvidenceStore, artifact: Any) -> None:
        """Seed a pre-existing artifact into the evidence store (idempotent).

        We go through add_observation by constructing a synthetic succeeded
        observation.  This ensures dedup and quality gates apply normally.
        """
        from ..contracts import ToolObservation

        obs = ToolObservation(
            tool_name=self._tool_for_artifact_type(artifact.artifact_type),
            status="succeeded",
            output={
                "source_url": artifact.source_url or "",
                "content_hash": artifact.content_hash or "",
                **(artifact.content or {}),
            },
        )
        store.add_observation(obs)

    @staticmethod
    def _tool_for_artifact_type(artifact_type: str) -> str:
        mapping = {
            "public_job_page": "fetch-public-job-pages",
            "structured_job_details": "extract-observed-job-details-batch",
            "job_matching_report": "match-observed-jobs",
            "resume_tailoring_brief": "build-resume-tailoring-brief",
        }
        return mapping.get(artifact_type, "fetch-public-job-pages")

    # ------------------------------------------------------------------
    # Per-skill delegation runner factory
    # ------------------------------------------------------------------

    def _make_runner_for_skill(
        self,
        *,
        skill: str,
        state: RunState,
        store: EvidenceStore,
        tracker: BudgetTracker,
        guard: ToolCallGuard,
        event_log: EventLogger,
        hooks: ControllerHooks,
        allowed_skills: tuple[str, ...],
        attempt_id: str,
        halt_box: list[tuple[str, str] | None],
    ) -> DelegationRunner:
        """Build a sync DelegationRunner bound to one specific skill.

        The runner executes via ``asyncio.to_thread`` in the delegation tool,
        so it must be a sync callable.  It uses ``asyncio.run`` internally to
        drive the skill agent loop.
        """

        def runner(task_goal: str, params: dict[str, Any]) -> DelegationOutcome:
            return asyncio.run(
                self._run_skill_delegation(
                    skill=skill,
                    task_goal=task_goal,
                    params=params,
                    state=state,
                    store=store,
                    tracker=tracker,
                    guard=guard,
                    event_log=event_log,
                    hooks=hooks,
                    allowed_skills=allowed_skills,
                    attempt_id=attempt_id,
                    halt_box=halt_box,
                )
            )

        return runner

    # ------------------------------------------------------------------
    # Skill delegation (async, run inside asyncio.run by the sync runner)
    # ------------------------------------------------------------------

    async def _run_skill_delegation(
        self,
        *,
        skill: str,
        task_goal: str,
        params: dict[str, Any],
        state: RunState,
        store: EvidenceStore,
        tracker: BudgetTracker,
        guard: ToolCallGuard,
        event_log: EventLogger,
        hooks: ControllerHooks,
        allowed_skills: tuple[str, ...],
        attempt_id: str,
        halt_box: list[tuple[str, str] | None],
    ) -> DelegationOutcome:
        """Run one skill delegation — the real skill agent loop."""
        del params

        # a. Validation (§6.7).
        if skill not in allowed_skills:
            return DelegationOutcome(
                skill=skill,
                status="error",
                error_code=DELEGATION_SKILL_NOT_ALLOWED,
            )

        if skill in state.completed_skills:
            return DelegationOutcome(
                skill=skill,
                status="error",
                error_code=DELEGATION_SKILL_ALREADY_SUCCEEDED,
            )

        # Skill-specific evidence prerequisites.
        if not _skill_has_evidence(skill, store):
            return DelegationOutcome(
                skill=skill,
                status="error",
                error_code=TARGET_EVIDENCE_NOT_FOUND,
            )

        # b. Fresh skill agent per delegation (§4.2).
        ctx = ToolContext(
            user_id=state.synthetic_user_id,
            run_id=state.run_id,
            attempt_id=attempt_id,
            skill_name=skill,
        )
        skill_agent = build_skill_agent(
            skill,
            self._model,
            ctx,
            registry=self._registry,
            stream_fn=hooks.stream_fn,
            get_api_key=self._get_api_key,
            before_tool_call=hooks.before_tool_call,
            after_tool_call=hooks.after_tool_call,
            should_stop_after_turn=hooks.should_stop_after_turn,
        )

        # c. Reset stall streak on delegation boundary (§7.2).
        guard.reset_stall_on_delegation()

        # d. Drive the skill agent.
        hooks.agent_ref_box[0] = skill_agent
        await skill_agent.prompt(task_goal)

        # If a halt fired inside the skill agent → return as error.
        if halt_box[0] is not None:
            halt_code, halt_msg = halt_box[0]
            return DelegationOutcome(
                skill=skill,
                status="error",
                error_code=halt_code,
                summary=halt_msg,
            )

        # e. Success: extract bounded summary + refs, check completion.
        final_text = _last_assistant_text(skill_agent)
        bounded = _bounded_summary(final_text)
        refs = store.refs()

        checker = _SKILL_CHECKERS.get(skill)
        if checker is not None and checker(store):
            state.completed_skills.add(skill)

        return DelegationOutcome(
            skill=skill,
            status="succeeded",
            summary=bounded,
            refs=refs,
        )

    # ------------------------------------------------------------------
    # Outcome decision
    # ------------------------------------------------------------------

    def _decide_outcome(
        self,
        *,
        state: RunState,
        store: EvidenceStore,
        tracker: BudgetTracker,
        guard: ToolCallGuard,
        supervisor: Agent,
        request: RunRequest,
        event_log: EventLogger,
        attempt_id: str,
        halt_box: list[tuple[str, str] | None],
    ) -> tuple[str, str | None, str | None]:
        """Determine the attempt outcome after the supervisor driver returns."""
        # Halt takes precedence.
        if halt_box[0] is not None:
            return ("waiting_user", halt_box[0][0], halt_box[0][1])

        # Final assistant message stop_reason check.
        last_msg = (
            supervisor.state.messages[-1]
            if supervisor.state.messages
            else None
        )
        if (
            last_msg is not None
            and isinstance(last_msg, AssistantMessage)
            and last_msg.stop_reason in ("error", "aborted")
        ):
            return (
                "waiting_user",
                INVALID_MODEL_RESPONSE,
                last_msg.error_message or "invalid model response",
            )

        # Matching fallback (§6.6) — deterministic one-shot.
        if _needs_matching_fallback(state, store, request):
            kernel_ctx = ToolContext(
                user_id=state.synthetic_user_id,
                run_id=state.run_id,
                attempt_id=attempt_id,
                skill_name=None,
            )
            with contextlib.suppress(CareerToolError):
                matching_fallback(
                    self._registry, kernel_ctx, store, tracker, guard
                )
            # If the fallback produced a report, mark matching as completed so
            # state.completed_skills and the final result reflect it accurately.
            if matching_completed(store):
                state.completed_skills.add("job-matching")

        # Extract summary.
        final_text = _last_assistant_text(supervisor)
        summary = _bounded_summary(final_text)
        if not summary:
            summary = state.summary

        state.summary = summary
        state.summary_refs = store.refs()

        # Run completion policy.
        status, code = RunCompletionPolicy().evaluate(state, store, summary)
        return (status, code, None)

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    def _finalize(
        self,
        *,
        state: RunState,
        store: EvidenceStore,
        tracker: BudgetTracker,
        event_log: EventLogger,
        status: str,
        error_code: str | None,
        error_message: str | None,
        attempt_count: int,
    ) -> RunResult:
        """Transition state to terminal, append final event, return result."""
        transition(
            state,
            RunStatus(status),
            summary=state.summary,
            error_code=error_code,
            error_message=error_message,
        )

        event_log.append(
            "run_finalized",
            {
                "status": status,
                "error_code": error_code or "",
                "attempt_count": attempt_count,
                "completed_skills": sorted(state.completed_skills),
            },
        )

        return RunResult(
            run_id=state.run_id,
            status=status,
            summary=state.summary,
            error_code=error_code,
            error_message=error_message,
            attempt_count=attempt_count,
            completed_skills=sorted(state.completed_skills),
            refs=store.refs(),
            events=event_log.events(),
            budget=tracker.consumed(),
        )


# ======================================================================
# Module-level helpers
# ======================================================================


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


def _skill_has_evidence(skill: str, store: EvidenceStore) -> bool:
    """Check whether *skill*'s prerequisite evidence exists (§6.7)."""
    if skill == "job-discovery":
        return True  # no prerequisite
    if skill == "job-matching":
        # Need at least one structured_job_details artifact with real candidates.
        for art in store.job_bearing_artifacts():
            if art.artifact_type == "structured_job_details":
                return True
        return False
    if skill == "resume-tailoring":
        # Need at least one job-bearing artifact.
        return bool(store.job_bearing_artifacts())
    return True


def _last_assistant_text(agent: Any) -> str | None:
    """Extract text from the last AssistantMessage in agent state."""
    for message in reversed(agent.state.messages):
        if isinstance(message, AssistantMessage):
            parts: list[str] = []
            for block in message.content:
                if hasattr(block, "text") and block.text:
                    parts.append(block.text)
            return "".join(parts) if parts else None
    return None


def _needs_matching_fallback(
    state: RunState,
    store: EvidenceStore,
    request: RunRequest,
) -> bool:
    """True when matching is needed but hasn't been produced yet, and
    structured candidates exist to match against."""
    needed = set(request.needed_skills or request.allowed_skills)
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
    "RunRequest",
    "RunResult",
    "CareerRunController",
]
