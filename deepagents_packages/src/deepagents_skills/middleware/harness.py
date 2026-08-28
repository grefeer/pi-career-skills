"""Harness middleware: pi hook semantics ported to langchain AgentMiddleware.

Four custom middlewares mirror ``pi_career_skills.runtime.agent_hooks``
(see MIGRATION.md §3 / §6):

* :class:`SequentialToolMiddleware` — force sequential tool execution
  (pi never runs business tools in parallel): keep only the first tool call
  of a multi-call model turn.
* :class:`BudgetMiddleware` — charge turn + model request per model call and
  input tokens + wall-clock per turn (pi ``stream_fn`` /
  ``should_stop_after_turn``); wall-clock exhaustion jumps to ``end``.
* :class:`EvidenceMiddleware` — the core: duplicate pre-check, tool-call
  budget admission, observation promotion into the EvidenceStore, tool-call
  guard (stall / repeated failure / soft-stop steer), external-failure
  counters and event logging (pi ``before_tool_call`` / ``after_tool_call``).
  Also logs ``delegation_*`` events when the supervisor's ``task`` tool
  returns a structured outcome.
* :class:`CompletionMiddleware` — supervisor only: after each model call,
  if a hard halt has been recorded, jump to ``end`` so the run terminates
  with a final AI message.

The remaining middlewares are official (langchain / deepagents): ModelRetry,
ToolRetry, ToolCallLimit, ModelCallLimit, ToolError, Summarization — see
:func:`build_middleware_stack` for the order and rationale.

All middleware is stateless; the run's mutable state lives in the
``HarnessState`` resolved from ``runtime.config["configurable"]["run_state"]``.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ModelRetryMiddleware,
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
    ToolErrorMiddleware,
    ToolRetryMiddleware,
    hook_config,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.constants import END
from langgraph.types import Command

from pi_career_skills.agents.contracts import DelegationOutcome
from pi_career_skills.contracts import Artifact
from pi_career_skills.errors import (
    DELEGATION_SKILL_ALREADY_SUCCEEDED,
    DELEGATION_SKILL_NOT_ALLOWED,
    DUPLICATE_TOOL_CALL,
    NO_PROGRESS,
    TARGET_EVIDENCE_NOT_FOUND,
    WALL_CLOCK_BUDGET_EXHAUSTED,
    CareerToolError,
)
from pi_career_skills.runtime.completion import (
    career_planning_completed,
    discovery_completed,
    matching_completed,
    tailoring_completed,
)
from pi_career_skills.runtime.evidence import canonical_json

from ..gates import (
    matching_explicit_no_match,
    role_plan_allowed,
    skill_has_evidence,
)
from ..models import ScriptedFakeChatModel
from ..run_state import SOFT_STALL_WRAP_UP, HarnessState, get_run_state

#: Per-skill completion checker (authoritative — store, not model output).
_SKILL_CHECKERS: dict[str, Any] = {
    "job-discovery": discovery_completed,
    "job-matching": matching_completed,
    "resume-tailoring": tailoring_completed,
    "career-planning": career_planning_completed,
}

#: Official limit middlewares default caps (MIGRATION.md §7).
_DEFAULT_TOOL_CALL_LIMIT = 40
_DEFAULT_MODEL_CALL_LIMIT = 40

#: Consecutive kills of the same skill's delegation (no structured outcome —
#: terminated by the model-call budget) before the harness records a hard
#: halt so the supervisor stops re-delegating into the dead end and the
#: store-based completion decision takes over.
_DELEGATION_KILL_LIMIT = 2

#: UI/navigation label titles that are never real job titles (pi agent_hooks).
_DISCOVERY_NAV_LABEL_TITLES = frozenset(
    {
        "浏览职位", "查看全部", "招聘观察", "申请职位", "职位", "岗位",
        "职位列表", "岗位列表", "热门职位", "招聘职位", "首页", "招聘",
        "投递", "登录", "注册", "联系我们", "关于我们", "返回", "更多",
    }
)

#: Per-skill durable deliverable artifact types (pi ``_deliverable_ready``).
_DELIVERABLE_TYPES: dict[str, frozenset[str]] = {
    "job-discovery": frozenset({"public_job_page", "structured_job_details"}),
    "job-matching": frozenset({"job_matching_report"}),
    "resume-tailoring": frozenset({"resume_tailoring_brief"}),
    "career-planning": frozenset({"career_preparation_plan"}),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_message(
    tool_call: dict[str, Any], content: str, *, status: str = "error"
) -> ToolMessage:
    return ToolMessage(
        content=content,
        tool_call_id=tool_call.get("id") or "",
        name=tool_call.get("name") or "",
        status=status,
    )


def _blocked_command(
    tool_call: dict[str, Any], error_code: str, message: str
) -> Command:
    """A Command that replaces the tool output with a blocked/error envelope."""
    payload = json.dumps(
        {
            "status": "blocked",
            "error_code": error_code,
            "error_message": message,
        },
        ensure_ascii=False,
    )
    return Command(update={"messages": [_tool_message(tool_call, payload)]})


def _delegation_command(
    tool_call: dict[str, Any],
    *,
    skill: str,
    status: str,
    summary: str,
    error_code: str | None,
    action: str,
    refs: list[Any] | None = None,
) -> Command:
    """A ``task``-shaped Command carrying a DelegationOutcome envelope.

    The supervisor sees the exact JSON the deepagents task tool would have
    produced, so gated delegations (blocked / partial / succeeded-shortcut)
    flow through the normal delegation event path.
    """
    payload = json.dumps(
        dataclasses.asdict(
            DelegationOutcome(
                skill=skill,
                status=status,
                summary=summary,
                refs=refs or [],
                error_code=error_code,
                action=action,
                consumed_budget=None,
            )
        ),
        ensure_ascii=False,
    )
    return Command(update={"messages": [_tool_message(tool_call, payload)]})


def _real_discovery_candidate(artifact: Any, candidate: Any) -> bool:
    """Mirror pi ``_real_discovery_candidate`` (agent_hooks.py)."""
    if not isinstance(candidate, dict):
        return False
    title = candidate.get("title")
    if not isinstance(title, str):
        return False
    title = title.strip()
    if len(title) < 2 or title in _DISCOVERY_NAV_LABEL_TITLES:
        return False
    if not any(("一" <= ch <= "鿿") or (ch.isascii() and ch.isalpha()) for ch in title):
        return False
    body = (
        f"{candidate.get('responsibilities') or ''} "
        f"{candidate.get('requirements') or ''}"
    ).strip()
    if len(body) < 20:
        return False
    quality = getattr(artifact, "quality", None) or (
        getattr(artifact, "content", None) or {}
    ).get("quality")
    if quality is not None and quality not in {"job_bearing", "jd_complete"}:
        return False
    artifact_source = getattr(artifact, "source_url", None) or (
        getattr(artifact, "content", None) or {}
    ).get("source_url")
    if not isinstance(artifact_source, str) or not artifact_source.strip():
        return False
    candidate_sources = (
        candidate.get("source_url"),
        candidate.get("page_source_url"),
        candidate.get("apply_url"),
    )
    return (
        any(source == artifact_source for source in candidate_sources)
        or quality == "job_bearing"
    )


def _deliverable_ready(skill_name: str, artifacts: list[Artifact]) -> bool:
    """Mirror pi ``_deliverable_ready``: a durable deliverable is persisted.

    This is the skill's terminal contract — once it is satisfied the
    subagent loop ends (pi ``after_tool_call`` returns ``terminate=True``).
    """
    expected = _DELIVERABLE_TYPES.get(skill_name, set())
    for artifact in artifacts:
        if getattr(artifact, "artifact_type", None) not in expected:
            continue
        content = getattr(artifact, "content", None) or {}
        if skill_name == "job-discovery":
            if artifact.artifact_type == "public_job_page" and (
                content.get("quality") == "jd_complete"
                or getattr(artifact, "quality", None) == "jd_complete"
            ):
                return True
            candidates = content.get("candidates") or content.get("details") or []
            if any(
                _real_discovery_candidate(artifact, candidate)
                for candidate in candidates
            ):
                return True
        elif skill_name == "job-matching":
            matches = content.get("matches")
            if isinstance(matches, list) and matches:
                return True
            if (
                content.get("no_match_reason")
                == "no_candidate_satisfied_constraints"
                and isinstance(content.get("evaluated_candidate_count"), int)
                and not isinstance(content.get("evaluated_candidate_count"), bool)
                and content["evaluated_candidate_count"] >= 0
            ):
                return True
        elif skill_name == "resume-tailoring":
            if content.get("target_artifact_id") and content.get("safe_actions"):
                return True
        elif skill_name == "career-planning":
            if content.get("target_artifact_id") and (
                content.get("plan_items") or content.get("actions")
            ):
                return True
    return False


def pre_delegation_gate(state: HarnessState, tool_call: dict[str, Any]) -> Command | None:
    """Mirror pi's per-skill delegation validation before the subagent runs.

    Returns a ``Command`` that short-circuits the ``task`` call, or ``None``
    to let the delegation proceed.  (pi validates inside the delegation
    runner; here the supervisor middleware gates on the ``task`` tool itself.)
    """
    args = tool_call.get("args") or {}
    skill = args.get("subagent_type") or ""
    if skill not in state.allowed_skills:
        return _delegation_command(
            tool_call,
            skill=skill,
            status="blocked",
            summary=f"skill {skill} is not allowed for this run",
            error_code=DELEGATION_SKILL_NOT_ALLOWED,
            action="stop",
        )
    if skill in state.kernel.completed_skills:
        return _delegation_command(
            tool_call,
            skill=skill,
            status="partial",
            summary=f"{skill} already succeeded with durable evidence",
            error_code=DELEGATION_SKILL_ALREADY_SUCCEEDED,
            action="stop",
        )
    # Resume-tailoring is conditionally not applicable when matching produced
    # an explicit, evidence-bound no-match result (pi controller shortcut).
    if skill == "resume-tailoring" and matching_explicit_no_match(state.store):
        state.kernel.completed_skills.add(skill)
        return _delegation_command(
            tool_call,
            skill=skill,
            status="succeeded",
            summary="匹配结果已明确没有满足约束的岗位，本轮没有可凭证据定制的目标 JD。",
            error_code=None,
            action="stop",
            refs=state.store.refs(),
        )
    task_goal = args.get("description") or skill
    if not skill_has_evidence(skill, state.store) and not (
        skill == "career-planning" and role_plan_allowed(task_goal)
    ):
        return _delegation_command(
            tool_call,
            skill=skill,
            status="blocked",
            summary=f"{skill} 缺少必要的持久化证据（TARGET_EVIDENCE_NOT_FOUND）",
            error_code=TARGET_EVIDENCE_NOT_FOUND,
            action="stop",
        )
    return None


def _usage_input_tokens(message: BaseMessage) -> int:
    """Sum input-side token counts from ``usage_metadata`` (0 if absent)."""
    usage = getattr(message, "usage_metadata", None) or {}
    if not isinstance(usage, dict):
        return 0
    return int(
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens") or 0)
        + (
            usage.get("cache_creation_input_tokens")
            or usage.get("cache_creation_tokens")
            or 0
        )
    )


# ---------------------------------------------------------------------------
# 1. SequentialToolMiddleware
# ---------------------------------------------------------------------------


class SequentialToolMiddleware(AgentMiddleware):
    """Keep only the first tool call of a multi-call model turn.

    Mirrors pi's sequential business-tool execution: the model may emit
    several parallel calls, but only the first is executed this turn; the
    rest are naturally re-requested on the next turn once results arrive.
    """

    async def awrap_model_call(self, request, handler):
        response = await handler(request)
        if hasattr(response, "result"):  # ModelResponse
            messages = response.result or []
            if len(messages) == 1 and isinstance(messages[0], AIMessage):
                first = messages[0]
                if len(first.tool_calls) > 1:
                    narrowed = first.model_copy(
                        update={"tool_calls": first.tool_calls[:1]}
                    )
                    return response.__class__(
                        result=[narrowed],
                        structured_response=getattr(response, "structured_response", None),
                    )
            return response
        if isinstance(response, AIMessage) and len(response.tool_calls) > 1:
            return response.model_copy(update={"tool_calls": response.tool_calls[:1]})
        return response


# ---------------------------------------------------------------------------
# 2. BudgetMiddleware
# ---------------------------------------------------------------------------


class BudgetMiddleware(AgentMiddleware):
    """Charge budget per model call and per turn; halt on wall-clock.

    Mirrors pi ``stream_fn`` (turn + model request) and
    ``should_stop_after_turn`` (input tokens + wall clock).  When the wall
    clock is exhausted the run jumps to ``end`` with a final AI message.
    """

    async def awrap_model_call(self, request, handler):
        state = get_run_state(request.runtime)
        if state is not None:
            try:
                state.tracker.consume_turn()
                state.tracker.consume_model_request(tokens=0)
            except CareerToolError as exc:
                state.halt(exc.code, exc.message)
                return AIMessage(
                    content=f"预算已耗尽（{exc.code}），请基于现有证据直接给出最终结论并结束。"
                )
        return await handler(request)

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state, runtime):
        harness = get_run_state(runtime)
        if harness is None:
            return None

        messages = state.get("messages") or []
        if messages and isinstance(messages[-1], BaseMessage):
            input_tokens = _usage_input_tokens(messages[-1])
            if input_tokens > 0:
                try:
                    harness.tracker.consume_input_tokens(input_tokens)
                except CareerToolError as exc:
                    harness.halt(exc.code, exc.message)

        if harness.tracker.wall_clock_exhausted():
            harness.halt(
                WALL_CLOCK_BUDGET_EXHAUSTED, "wall clock budget exhausted"
            )
            return {
                "jump_to": "end",
                "messages": [AIMessage(content="wall clock budget exhausted")],
            }
        return None


# ---------------------------------------------------------------------------
# 3. EvidenceMiddleware
# ---------------------------------------------------------------------------


class EvidenceMiddleware(AgentMiddleware):
    """pi before/after-tool-call semantics on the deepagents tool boundary.

    Wraps every tool call (business tools in skill subagents, ``task``
    delegations on the supervisor).  Business-tool observations are promoted
    into the shared EvidenceStore, the ToolCallGuard tracks stall/duplicates
    and the run's external-failure counters mirror agent_hooks exactly.
    """

    #: Failure codes that count toward a "blocked public source" hand-off.
    _BLOCKED_SOURCE_CODES = frozenset(
        {"anti_bot_challenge", "captcha", "login_required", "needs_manual_review"}
    )
    #: Route-level failures that become a hand-off when repeated with no
    #: usable evidence.
    _ROUTE_EXHAUSTED_CODES = frozenset({"route_already_consumed", "wechat_ocr_disabled"})
    #: Deterministic miss codes that halt as ``no_progress`` after 3 repeats.
    _MISS_CODES = frozenset(
        {
            "target_evidence_not_found",
            "target_role_mismatch",
            "target_source_mismatch",
            "empty_public_page",
            "public_page_content_insufficient",
            "invalid_tool_input",
        }
    )

    def __init__(self, *, skill_name: str | None = None) -> None:
        super().__init__()
        self.skill_name = skill_name
        self.kind_label = skill_name or "supervisor"
        #: Set when this subagent promoted a durable deliverable (pi's
        #: ``_deliverable_ready`` terminate).  The after_model hook reads it
        #: to end the subagent loop with a structured success outcome.
        self._deliverable_ready = False

    # -- after-model: deliverable termination ------------------------------

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state, runtime):
        """Subagent only: end the loop once a durable deliverable is persisted.

        Mirrors pi ``after_tool_call``'s ``_deliverable_ready`` terminate: a
        skill deliverable in the store is the skill's terminal contract, so
        the subagent has nothing more to produce.  Jump to ``end`` carrying
        the structured DelegationOutcome; the supervisor then receives a
        proper task result and can re-delegate the next required skill
        instead of the subagent looping into a no-progress stall.
        """
        harness = get_run_state(runtime)
        if harness is None or self.skill_name is None:
            return None
        if not self._deliverable_ready or harness.halt_code is not None:
            # No deliverable, or a hard halt is already recorded — the halt
            # flow (run-level outcome) owns the termination decision.
            return None
        outcome = DelegationOutcome(
            skill=self.skill_name,
            status="success",
            summary=(
                f"{self.skill_name} 已产出持久化交付物，"
                f"共 {len(harness.store.job_bearing_artifacts())} 条岗位证据入库。"
            ),
            refs=harness.store.refs(),
            error_code=None,
            action="continue",
            consumed_budget=None,
        )
        harness.event_log.append(
            "deliverable_ready",
            {
                "skill": self.skill_name,
                "artifacts": len(harness.store.job_bearing_artifacts()),
            },
        )
        return {
            "jump_to": "end",
            "structured_response": outcome,
            "messages": [
                AIMessage(
                    content=f"{self.skill_name} 交付物已持久化，技能完成，返回结构化结果。"
                )
            ],
        }

    # -- before handler ---------------------------------------------------

    def _before_call(
        self,
        state: HarnessState,
        tool_call: dict[str, Any],
    ) -> Command | None:
        """Duplicate pre-check + tool-call budget admission; None = proceed."""
        name = tool_call.get("name") or ""
        args = tool_call.get("args") or {}
        params_hash = canonical_json(args)

        if state.halt_code is not None:
            return _blocked_command(
                tool_call, "run_halting", f"run halted: {state.halt_code}"
            )
        if state.guard.is_duplicate(name, params_hash):
            return _blocked_command(
                tool_call, DUPLICATE_TOOL_CALL, f"duplicate tool call: {name}"
            )
        try:
            state.tracker.consume_tool_call(state.guard.artifact_count)
        except CareerToolError as exc:
            state.halt(exc.code, exc.message)
            return _blocked_command(tool_call, exc.code, exc.message)
        return None

    # -- after handlers ---------------------------------------------------

    def _handle_business_result(
        self,
        state: HarnessState,
        tool_call: dict[str, Any],
        params_hash: str,
        result: ToolMessage,
    ) -> Command | ToolMessage:
        """Promote an observation, drive the guard and log events."""
        call_id = tool_call.get("id") or ""
        name = tool_call.get("name") or ""
        obs = state.take_observation(call_id)

        if obs is None:
            # Synthetic ToolMessage (not produced by a CareerLangchainTool) —
            # nothing to promote, pass through.
            return result

        promoted = state.store.add_observation(obs)
        state.guard.set_artifact_count(len(state.store.job_bearing_artifacts()))
        succeeded = obs.status == "succeeded"
        produced = bool(promoted)
        error_code = obs.error_code or ""
        if (
            self.skill_name
            and produced
            and _deliverable_ready(self.skill_name, promoted)
        ):
            # Terminal contract satisfied — the after_model hook ends the
            # subagent loop on the next model boundary (pi terminates here).
            self._deliverable_ready = True

        # External-failure counters (mirrors agent_hooks.after_tool_call).
        terminate = self._update_external_failures(state, succeeded, error_code, produced)

        try:
            signal = state.guard.note_call(
                name, params_hash, succeeded=succeeded, produced_artifact=produced
            )
        except CareerToolError as exc:
            state.halt(exc.code, exc.message)
            return _blocked_command(tool_call, exc.code, exc.message)

        if signal == "repeated_tool_failure":
            state.halt(NO_PROGRESS, f"repeated failure for {name}")
            return _blocked_command(tool_call, NO_PROGRESS, f"repeated failure for {name}")

        # Soft stall — steer ONCE with the wrap-up message (pi steers the
        # agent mid-loop; here we append the steer as a HumanMessage next to
        # the tool result).
        if signal == "soft_stop" and not state.soft_stall_steered:
            state.soft_stall_steered = True
            state.event_log.append(
                "stall_soft_warning",
                {"kind": self.kind_label, "streak": state.guard.stall_streak},
            )
            steer = HumanMessage(content=SOFT_STALL_WRAP_UP)
            return Command(update={"messages": [result, steer]})

        state.event_log.append(
            "tool_observation",
            {
                "tool_name": name,
                "status": obs.status,
                "error_code": error_code,
                "error_message": getattr(obs, "error_message", "") or "",
                "promoted_artifacts": len(promoted),
            },
        )
        if terminate:
            return Command(update={"messages": [result]}, goto=END)
        return result

    def _update_external_failures(
        self, state: HarnessState, succeeded: bool, error_code: str, produced: bool
    ) -> bool:
        """Mirror agent_hooks' external hand-off counters; True = stop run."""
        if succeeded:
            if produced:
                # New evidence resets stale miss streaks.
                for key in tuple(state.external_failure_counts):
                    if key.startswith("miss:"):
                        state.external_failure_counts.pop(key, None)
            return False
        counts = state.external_failure_counts
        if error_code in self._BLOCKED_SOURCE_CODES:
            counts["blocked_public_source"] = counts.get("blocked_public_source", 0) + 1
            if counts["blocked_public_source"] >= 2 and not self._has_usable_evidence(state):
                state.halt(
                    error_code, f"{error_code}: no usable public evidence remains"
                )
                return True
        elif error_code in self._ROUTE_EXHAUSTED_CODES:
            counts[error_code] = counts.get(error_code, 0) + 1
            if counts[error_code] >= 2 and not self._has_usable_evidence(state):
                state.halt(
                    error_code,
                    f"{error_code}: route exhausted without a productive next step",
                )
                return True
        elif error_code in self._MISS_CODES:
            miss_key = f"miss:{error_code}"
            counts[miss_key] = counts.get(miss_key, 0) + 1
            if counts[miss_key] >= 3:
                state.halt(
                    NO_PROGRESS, f"{error_code}: repeated failure without new evidence"
                )
                return True
        return False

    @staticmethod
    def _has_usable_evidence(state: HarnessState) -> bool:
        return bool(state.store.usable_evidence_artifacts())

    def _handle_delegation(
        self,
        state: HarnessState,
        tool_call: dict[str, Any],
        params_hash: str,
        command: Command,
    ) -> None:
        """Log delegation events from the supervisor's ``task`` result."""
        outcome: dict[str, Any] | None = None
        update = command.update
        if isinstance(update, dict):
            for message in reversed(update.get("messages") or []):
                if isinstance(message, ToolMessage) and isinstance(message.content, str):
                    try:
                        parsed = json.loads(message.content)
                    except (TypeError, ValueError):
                        parsed = None
                    if isinstance(parsed, dict):
                        outcome = parsed
                    break
        if not outcome or "skill" not in outcome:
            # The subagent ended without a structured DelegationOutcome — it
            # was killed by the model-call budget (or an error), so its plain
            # result is invisible to the stall guard below and the supervisor
            # would happily re-delegate into the same dead end until the
            # wall-clock backstop.  Count consecutive kills per skill; on the
            # second, record a hard halt so CompletionMiddleware ends the
            # graph and the controller's store-based completion decision
            # takes over (the store is the authority, not the model).
            if (tool_call.get("name") or "") == "task":
                killed_skill = (tool_call.get("args") or {}).get("subagent_type") or ""
                if killed_skill:
                    kills = state.killed_delegation_counts.get(killed_skill, 0) + 1
                    state.killed_delegation_counts[killed_skill] = kills
                    state.event_log.append(
                        "delegation_killed",
                        {"skill": killed_skill, "kill_count": kills},
                    )
                    if kills >= _DELEGATION_KILL_LIMIT:
                        state.halt(
                            "budget_exhausted",
                            f"subagent {killed_skill!r} repeatedly terminated by "
                            "the model-call budget",
                        )
            return
        skill = outcome.get("skill", "")
        status = outcome.get("status", "")
        succeeded = status in {"succeeded", "success"}
        # Authoritative completion is decided from the store, not the model's
        # self-report — mirror pi's delegation runner.
        if succeeded:
            checker = _SKILL_CHECKERS.get(skill)
            if checker is not None and checker(state.store):
                state.kernel.completed_skills.add(skill)
        try:
            state.guard.note_call(
                f"delegate-{skill}",
                params_hash,
                succeeded=succeeded,
                produced_artifact=False,
            )
        except CareerToolError as exc:
            state.halt(exc.code, exc.message)
            return
        state.event_log.append(
            f"delegation_{status}",
            {"skill": skill, "error_code": outcome.get("error_code") or ""},
        )

    # -- the wrapper ------------------------------------------------------

    async def awrap_tool_call(self, request, handler):
        state = get_run_state(request.runtime)
        tool_call = request.tool_call
        params_hash = canonical_json(tool_call.get("args") or {})

        if state is None:
            return await handler(request)

        blocked = self._before_call(state, tool_call)
        if blocked is not None:
            return blocked

        # Supervisor delegation gate: validate before the subagent runs (pi
        # validates inside its delegation runner).  The subagent never
        # starts for gated delegations — no budget is spent on it.
        if self.skill_name is None and (tool_call.get("name") or "") == "task":
            gated = pre_delegation_gate(state, tool_call)
            if gated is not None:
                return gated
            # A real delegation is progress — reset the stall streak.
            state.guard.reset_stall_on_delegation()
            args = tool_call.get("args") or {}
            state.begin_delegation(
                args.get("subagent_type") or "",
                args.get("description") or args.get("subagent_type") or "",
            )
            delegation_started = True
        else:
            delegation_started = False

        try:
            result = await handler(request)

            if isinstance(result, Command):
                # Supervisor delegation (task tool) — a Command with messages.
                self._handle_delegation(state, tool_call, params_hash, result)
                return result
            if isinstance(result, ToolMessage):
                return self._handle_business_result(state, tool_call, params_hash, result)
            if isinstance(result, list):
                # Rare: a tool returning multiple outputs — handle the first.
                for item in result:
                    if isinstance(item, ToolMessage):
                        return self._handle_business_result(
                            state, tool_call, params_hash, item
                        )
            return result
        finally:
            if delegation_started:
                state.end_delegation()


# ---------------------------------------------------------------------------
# 4. CompletionMiddleware (supervisor only)
# ---------------------------------------------------------------------------


class CompletionMiddleware(AgentMiddleware):
    """Supervisor-only gate: end the run once a hard halt has been recorded.

    Subagents keep their structured-outcome contract (they never jump to
    end themselves); the supervisor stops at the next model boundary.
    """

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state, runtime):
        harness = get_run_state(runtime)
        if harness is None or harness.halt_code is None:
            return None
        return {
            "jump_to": "end",
            "messages": [
                AIMessage(
                    content=f"run halted: {harness.halt_code}"
                    f" — {harness.halt_message or ''}"
                )
            ],
        }


# ---------------------------------------------------------------------------
# Stack assembly
# ---------------------------------------------------------------------------


def build_middleware_stack(
    *,
    skill_name: str | None = None,
    model: BaseChatModel | None = None,
    tool_call_limit: int = _DEFAULT_TOOL_CALL_LIMIT,
    model_call_limit: int = _DEFAULT_MODEL_CALL_LIMIT,
    summarization: bool = False,
) -> list[AgentMiddleware]:
    """Build the middleware stack (first = outermost).

    Ordering rationale (MIGRATION.md §7):

    1. ``SequentialTool`` — truncate multi-call turns first so inner layers
       never see parallel business calls.
    2. ``Budget`` — charge the model call before any inner layer runs.
    3. ``ToolError`` — outermost official net: convert any unexpected tool
       exception (including harness bugs) into an error ToolMessage.
    4. ``ToolRetry`` — retry transient tool exceptions (never fires for the
       error-envelope adapter, kept for policy completeness).
    5. ``Evidence`` — the harness core, closest to the tool.
    6. ``Completion`` — supervisor-only hard-halt gate.
    7. ``ModelRetry`` — retry transient model exceptions.
    8. ``ToolCallLimit`` — global cap on tool calls (official).
    9. ``ModelCallLimit`` — global cap on model calls (official).
    10. ``Summarization`` — context compaction when the conversation grows
        (official; uses the same model; disabled for the scripted faux model
        because its deterministic responses cannot summarize).

    ``Completion`` is only added for the supervisor (``skill_name is None``).
    """
    stack: list[AgentMiddleware] = [
        SequentialToolMiddleware(),
        BudgetMiddleware(),
        ToolErrorMiddleware(
            on_error=lambda exc, request: (
                f"`{request.tool_call['name']}` 执行失败：{type(exc).__name__}，"
                "请修正输入后重试。"
            )
        ),
        ToolRetryMiddleware(max_retries=1),
        EvidenceMiddleware(skill_name=skill_name),
    ]
    if skill_name is None:
        stack.append(CompletionMiddleware())
    stack.extend(
        [
            ModelRetryMiddleware(max_retries=1),
            ToolCallLimitMiddleware(
                tool_name=None,
                thread_limit=tool_call_limit,
                run_limit=tool_call_limit,
                exit_behavior="continue",
            ),
            ModelCallLimitMiddleware(
                thread_limit=model_call_limit,
                run_limit=model_call_limit,
                exit_behavior="end",
            ),
        ]
    )
    if summarization and not isinstance(model, ScriptedFakeChatModel) and model is not None:
        stack.append(SummarizationMiddleware(model=model))
    return stack


__all__ = [
    "BudgetMiddleware",
    "CompletionMiddleware",
    "EvidenceMiddleware",
    "SequentialToolMiddleware",
    "build_middleware_stack",
]
