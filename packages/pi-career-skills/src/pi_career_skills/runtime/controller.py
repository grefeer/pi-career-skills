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
from dataclasses import dataclass, replace
from typing import Any

from pi_ai import AssistantMessage, Model, ToolResultMessage
from pi_coding_agent import CodingAgent

from ..agents.capabilities import capability_budget_limits
from ..agents.contracts import AgentTask
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
    WALL_CLOCK_BUDGET_EXHAUSTED,
    CareerToolError,
    redact_message,
)
from ..registry import build_career_tool_registry
from .agent_hooks import ControllerHooks, build_controller_hooks
from .budgets import BudgetConsumed, BudgetLimits, BudgetTracker, ToolCallGuard
from .completion import (
    RunCompletionPolicy,
    _bounded_summary,
    career_planning_completed,
    discovery_completed,
    matching_completed,
    matching_fallback,
    tailoring_completed,
)
from .context_projection import (
    RuntimeContextProjection,
    safe_private_context,
    seed_artifact,
)
from .events import EventLogger
from .evidence import EvidenceStore
from .partial_answer import build_partial_answer
from .recovery import AUTO_RECOVERABLE_REASONS, should_auto_recover
from .state import RunState, RunStatus, transition

#: Per-skill completion checker (§6.5).
_SKILL_CHECKERS: dict[str, Callable[[Any], bool]] = {
    "job-discovery": discovery_completed,
    "job-matching": matching_completed,
    "resume-tailoring": tailoring_completed,
    "career-planning": career_planning_completed,
}


def _child_budget_limits(skill: str) -> BudgetLimits:
    """Convert registry defaults into a bounded child budget."""
    return capability_budget_limits(skill)


def _delegation_status_for_error(error_code: str) -> str:
    """Map trusted error codes to supervisor-decision states."""
    if error_code in {TARGET_EVIDENCE_NOT_FOUND, DELEGATION_SKILL_NOT_ALLOWED}:
        return "blocked"
    if error_code == DELEGATION_SKILL_ALREADY_SUCCEEDED:
        return "partial"
    if error_code in {
        "budget_exhausted",
        "no_progress",
        "timeout",
        "rate_limited",
        WALL_CLOCK_BUDGET_EXHAUSTED,
    }:
        return "retryable"
    if error_code in {"needs_user", "manual_review_required"}:
        return "need_user"
    return "failed"

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
        "career-planning",
    )
    needed_skills: tuple[str, ...] | None = None
    budget: BudgetLimits | None = None
    seed_artifacts: list[Any] | None = None
    private_context: dict[str, Any] | None = None


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
    artifacts: list[dict[str, Any]]
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
        models: dict[str, Model] | None = None,
    ) -> None:
        self._model = model
        self._models = dict(models or {})
        self._registry = (
            registry if registry is not None else build_career_tool_registry()
        )
        self._get_api_key = get_api_key or _default_get_api_key

    def _model_for(self, agent_name: str) -> Model:
        """Resolve an optional per-agent model, falling back to the run model."""
        return self._models.get(agent_name, self._model)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, request: RunRequest) -> RunResult:
        """Execute one run and return the terminal result."""
        # 1. API key check — fail fast for deepseek without a key.
        api_key = self._get_api_key(self._model.provider)
        if self._model.provider == "deepseek" and not api_key:
            return self._failed_key_missing(request)
        for routed_model in self._models.values():
            if routed_model.provider == "deepseek" and not self._get_api_key(routed_model.provider):
                return self._failed_key_missing(request, provider=routed_model.provider)

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
            attempt_artifact_count = len(store.job_bearing_artifacts())
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
            private_ctx = safe_private_context(request.private_context)
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
                    private_context=private_ctx,
                )

            # Build fresh supervisor via the factory.
            supervisor = build_supervisor_agent(
                self._model_for("supervisor"),
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
                await asyncio.wait_for(
                    supervisor.prompt(request.task),
                    timeout=max(0.1, tracker.remaining_wall_clock_seconds()),
                )

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
                    await asyncio.wait_for(
                        supervisor.continue_(),
                        timeout=max(0.1, tracker.remaining_wall_clock_seconds()),
                    )

                # Some pi-agent-core versions suppress the supervisor
                # callback for legacy terminal delegate results.  Completion
                # state is authoritative, so retain an auditable event.
                observed_events = event_log.events()
                if state.completed_skills and not any(
                    event.type.startswith("delegation_")
                    for event in observed_events
                ):
                    for completed_skill in sorted(state.completed_skills):
                        event_log.append(
                            "delegation_success",
                            {"skill": completed_skill, "error_code": ""},
                        )

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
            except TimeoutError:
                outcome_status = "waiting_user"
                outcome_code = WALL_CLOCK_BUDGET_EXHAUSTED
                outcome_msg = "run wall-clock budget exhausted"
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
                # Recovery must change the evidence state or the route.  A
                # fresh model attempt with the same evidence only repeats the
                # failure (the observed Q034/Q040/Q046/Q148/R024/R025/R043
                # pattern).  Keep the trusted partial artifacts and hand off
                # instead of spending another full supervisor attempt.
                attempt_added_artifacts = (
                    len(store.job_bearing_artifacts()) - attempt_artifact_count
                )
                if (
                    attempt_added_artifacts <= 0
                    and outcome_code == "no_progress"
                ):
                    if store.job_bearing_artifacts() and not state.summary:
                        state.summary = (
                            f"已保留 {len(store.job_bearing_artifacts())} 条持久化证据；"
                            f"本轮因 {outcome_code} 未继续重复尝试。"
                        )
                    return self._finalize(
                        state=state,
                        store=store,
                        tracker=tracker,
                        event_log=event_log,
                        status="waiting_user",
                        error_code=outcome_code,
                        error_message=outcome_msg,
                        attempt_count=attempt_count,
                    )
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

    def _failed_key_missing(self, request: RunRequest, *, provider: str = "deepseek") -> RunResult:
        """Return a failed result with zero attempts — no model loop."""
        run_id = request.run_id or uuid.uuid4().hex
        return RunResult(
            run_id=run_id,
            status="failed",
            summary=None,
            error_code=MODEL_API_KEY_MISSING,
            error_message=f"API key missing for provider {provider}",
            attempt_count=0,
            completed_skills=[],
            refs=[],
            artifacts=[],
            events=[],
            budget=BudgetConsumed(),
        )

    # ------------------------------------------------------------------
    # Seed artifacts
    # ------------------------------------------------------------------

    def _seed_artifact(self, store: EvidenceStore, artifact: Any) -> None:
        """Seed a chain artifact through the runtime evidence boundary."""
        seed_artifact(store, artifact)

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
        private_context: dict[str, Any] | None = None,
    ) -> DelegationRunner:
        """Build a sync DelegationRunner bound to one specific skill.

        The runner executes via ``asyncio.to_thread`` in the delegation tool,
        so it must be a sync callable.  It uses ``asyncio.run`` internally to
        drive the skill agent loop.
        """
        captured_private = safe_private_context(private_context)
        retry_counts: dict[tuple[str, str | None], int] = {}

        def runner(task_goal: AgentTask | str, params: dict[str, Any]) -> DelegationOutcome:
            event_count = len(event_log.events())
            outcome = asyncio.run(
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
                    private_context=captured_private,
                )
            )
            if outcome.status.value == "retryable" and outcome.error_code:
                retry_key = (skill, outcome.error_code)
                retry_counts[retry_key] = retry_counts.get(retry_key, 0) + 1
                if retry_counts[retry_key] >= 2:
                    # Repeating the same retryable failure without new
                    # evidence is no longer productive.  Surface BLOCKED so
                    # the supervisor asks the user or changes route instead
                    # of spending the run budget in an identical loop.
                    outcome = replace(
                        outcome,
                        status="blocked",
                        error_code="delegation_retry_limit",
                        summary=(
                            outcome.summary
                            or f"{skill}: repeated {outcome.error_code or 'retryable'} failure"
                        ),
                    )
            # pi-agent-core does not invoke ``after_tool_call`` for legacy
            # terminal tool results.  Record the delegation at this runtime
            # boundary as a fallback, while avoiding duplicates when the hook
            # already observed a structured result.
            new_events = event_log.events()[event_count:]
            if not any(event.type.startswith("delegation_") for event in new_events):
                event_log.append(
                    f"delegation_{outcome.status.value}",
                    {
                        "skill": skill,
                        "error_code": outcome.error_code or "",
                    },
                )
            return outcome

        return runner

    # ------------------------------------------------------------------
    # Skill delegation (async, run inside asyncio.run by the sync runner)
    # ------------------------------------------------------------------

    async def _run_skill_delegation(
        self,
        *,
        skill: str,
        task_goal: AgentTask | str,
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
        private_context: dict[str, Any] | None = None,
    ) -> DelegationOutcome:
        """Run one skill delegation — the real skill agent loop."""
        del params
        task = task_goal if isinstance(task_goal, AgentTask) else AgentTask(objective=task_goal)

        # a. Validation (§6.7).
        if skill not in allowed_skills:
            return DelegationOutcome(
                skill=skill,
                status=_delegation_status_for_error(DELEGATION_SKILL_NOT_ALLOWED),
                error_code=DELEGATION_SKILL_NOT_ALLOWED,
            )

        if skill in state.completed_skills:
            return DelegationOutcome(
                skill=skill,
                status=_delegation_status_for_error(DELEGATION_SKILL_ALREADY_SUCCEEDED),
                error_code=DELEGATION_SKILL_ALREADY_SUCCEEDED,
            )

        # Tailoring is conditionally not applicable when matching produced an
        # explicit, evidence-bound no-match result.  Treat that as a truthful
        # terminal outcome for the dependent skill instead of repeatedly
        # inventing a target JD that does not exist.
        if skill == "resume-tailoring" and _matching_explicit_no_match(store):
            state.completed_skills.add(skill)
            return DelegationOutcome(
                skill=skill,
                status="succeeded",
                summary="匹配结果已明确没有满足约束的岗位，本轮没有可凭证据定制的目标 JD。",
                refs=store.refs(),
            )

        # Skill-specific evidence prerequisites.
        if not _skill_has_evidence(skill, store) and not (
            skill == "career-planning" and _role_plan_allowed(task.objective)
        ):
            return DelegationOutcome(
                skill=skill,
                status=_delegation_status_for_error(TARGET_EVIDENCE_NOT_FOUND),
                error_code=TARGET_EVIDENCE_NOT_FOUND,
            )

        projection = RuntimeContextProjection(private_context)
        ctx = ToolContext(
            user_id=state.synthetic_user_id,
            run_id=state.run_id,
            attempt_id=attempt_id,
            skill_name=skill,
            metadata=projection.initial_metadata(store),
        )
        ctx.metadata["task_goal"] = task.objective
        ctx.metadata["delegation_task"] = task.to_dict()
        # Network calls are governed in production: same-domain requests are
        # serialized, successful pages are reused, and a challenge opens a
        # cooldown circuit.  Direct unit-tool contexts may omit this flag so
        # deterministic tests never sleep or share process cache state.
        ctx.metadata.setdefault("enforce_public_request_governor", True)
        ctx.metadata.setdefault("public_request_interval_seconds", 2.5)
        ctx.metadata.setdefault("public_page_cache_ttl_seconds", 6 * 60 * 60)
        ctx.metadata.setdefault("public_block_cooldown_seconds", 30 * 60)
        def refresh_callback() -> None:
            projection.refresh(ctx.metadata, store)

        hooks.context_refresh_box[0] = refresh_callback
        child_limits = _child_budget_limits(skill)
        child_tracker = (
            tracker.child(child_limits)
            if hasattr(tracker, "child")
            else tracker
        )
        skill_hooks = build_controller_hooks(
            tracker=child_tracker,
            guard=guard,
            store=store,
            event_log=event_log,
            halt_box=halt_box,
        )
        skill_hooks.context_refresh_box[0] = refresh_callback
        if hasattr(child_tracker, "mark_attempt_started"):
            child_tracker.mark_attempt_started()
        try:
            skill_agent = build_skill_agent(
                skill,
                self._model_for(skill),
                ctx,
                registry=self._registry,
                stream_fn=skill_hooks.stream_fn,
                get_api_key=self._get_api_key,
                before_tool_call=skill_hooks.before_tool_call,
                after_tool_call=skill_hooks.after_tool_call,
                should_stop_after_turn=skill_hooks.should_stop_after_turn,
            )

            guard.reset_stall_on_delegation()

            # d. Drive the skill agent.
            skill_hooks.agent_ref_box[0] = skill_agent
            available_refs = [
                ref.get("artifact_id", "")
                for ref in store.refs()
                if ref.get("artifact_id")
            ][:12]
            skill_objective = task.objective
            if available_refs:
                skill_objective += (
                    "\n\n仅可从以下已持久化 evidence refs 中选择 target_artifact_id："
                    + ", ".join(available_refs)
                )
            skill_timeout = (
                max(0.1, child_tracker.remaining_wall_clock_seconds())
                if hasattr(child_tracker, "remaining_wall_clock_seconds")
                else 600.0
            )
            await asyncio.wait_for(
                skill_agent.prompt(skill_objective),
                timeout=skill_timeout,
            )

            # If a halt fired inside the skill agent → return as error.
            if halt_box[0] is not None:
                halt_code, halt_msg = halt_box[0]
                checker = _SKILL_CHECKERS.get(skill)
                if checker is not None and checker(store):
                    # A budget/stall signal may arrive after the final tool
                    # has already persisted a valid deliverable. The durable
                    # artifact is authoritative; do not discard it merely
                    # because the model attempted one extra turn.
                    state.completed_skills.add(skill)
                    return DelegationOutcome(
                        skill=skill,
                        status="succeeded",
                        summary=_bounded_summary(_last_assistant_text(skill_agent)),
                        refs=store.refs(),
                    )
                return DelegationOutcome(
                    skill=skill,
                    status=_delegation_status_for_error(halt_code),
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
        except TimeoutError:
            return DelegationOutcome(
                skill=skill,
                status="retryable",
                error_code=WALL_CLOCK_BUDGET_EXHAUSTED,
                summary="skill wall-clock budget exhausted",
            )
        finally:
            if hasattr(child_tracker, "mark_attempt_finished"):
                child_tracker.mark_attempt_finished()
            if hooks.context_refresh_box[0] is refresh_callback:
                hooks.context_refresh_box[0] = None
            if skill_hooks.context_refresh_box[0] is refresh_callback:
                skill_hooks.context_refresh_box[0] = None

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
        supervisor: CodingAgent,
        request: RunRequest,
        event_log: EventLogger,
        attempt_id: str,
        halt_box: list[tuple[str, str] | None],
    ) -> tuple[str, str | None, str | None]:
        """Determine the attempt outcome after the supervisor driver returns."""
        # A stall may fire immediately after the final durable deliverable is
        # promoted (for example, the supervisor keeps trying to re-delegate
        # after a planning artifact already completed).  Trusted completion
        # evidence wins in that narrow case; otherwise the halt remains a
        # genuine human hand-off.
        if halt_box[0] is not None:
            halt_code = halt_box[0][0]
            # Auto-recovery creates fresh agent hooks.  Preserve the trusted
            # external fact across attempts: once a public source has
            # explicitly returned an anti-bot/manual-review error, escalating
            # that same run to ``auto_recovery_limit_reached`` is misleading
            # and makes the user repeat an already-known handoff.
            if (
                halt_code in {
                    "auto_recovery_limit_reached",
                    "no_progress",
                    "route_already_consumed",
                    "budget_exhausted",
                    WALL_CLOCK_BUDGET_EXHAUSTED,
                }
                and any(
                    (getattr(event, "payload", {}) or {}).get("error_code")
                    in {"anti_bot_challenge", "captcha", "login_required", "needs_manual_review"}
                    for event in event_log.events()
                )
            ):
                return (
                    "waiting_user",
                    "anti_bot_challenge",
                    "public source requires manual review after an observed anti-bot challenge",
                )
            if (
                halt_code in {
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
            fallback_projection = RuntimeContextProjection(request.private_context)
            kernel_ctx = ToolContext(
                user_id=state.synthetic_user_id,
                run_id=state.run_id,
                attempt_id=attempt_id,
                skill_name=None,
                metadata=fallback_projection.initial_metadata(store),
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
        if (
            status == "waiting_user"
            and code == "completion_evidence_unavailable"
            and state.completed_skills.issuperset(request.needed_skills or ())
            and store.job_bearing_artifacts()
        ):
            # The model may omit a resolvable summary ref after a successful
            # delegated tool call. Trusted completion artifacts still prove
            # the requested work; synthesize a bounded audit summary instead
            # of forcing an unnecessary retry loop.
            state.summary = summary or "已根据持久化证据完成请求。"
            state.summary_refs = store.refs()
            return ("succeeded", None, None)
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
        # Non-succeeded runs still owe the user a text answer from the partial
        # evidence (budget/wall-clock/evidence-gate terminations).  The partial
        # answer is deterministic and source-backed — it never fabricates jobs.
        if status in {"waiting_user", "failed"}:
            partial = build_partial_answer(store, error_code=error_code)
            if partial:
                state.summary = partial
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
            artifacts=_serialize_artifacts(store),
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


def _serialize_artifacts(store: EvidenceStore) -> list[dict[str, Any]]:
    """Serialize all evidence-store artifacts into plain dicts (with content).

    Used by the evaluation chain runner to seed the next link's evidence
    store.  Each dict includes artifact_id, artifact_type, source_url,
    content_hash, quality, and the full content_json (for chain projection
    and seeding).
    """
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


def _skill_has_evidence(skill: str, store: EvidenceStore) -> bool:
    """Check whether *skill*'s prerequisite evidence exists (§6.7)."""
    if skill == "job-discovery":
        return True  # no prerequisite
    if skill == "job-matching":
        # Matching can score either structured candidates or a complete public
        # JD directly (``match_observed_jobs`` has a raw-evidence fallback).
        # Requiring extraction first made valid static campus/company pages
        # unusable whenever normalization failed, even though their persisted
        # visible text was sufficient and provenance-bound.
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
    if skill == "resume-tailoring":
        # Need at least one job-bearing artifact.
        return bool(store.job_bearing_artifacts())
    if skill == "career-planning":
        return bool(store.job_bearing_artifacts())
    return True


def _matching_explicit_no_match(store: EvidenceStore) -> bool:
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


def _role_plan_allowed(task_goal: str) -> bool:
    """Allow an explicit role-level plan when no employer JD is available."""
    if not isinstance(task_goal, str) or not task_goal.strip():
        return False
    lowered = task_goal.lower()
    return any(
        marker in lowered
        for marker in ("岗位准备计划", "面试准备计划", "求职准备计划", "职业准备计划")
    )


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
