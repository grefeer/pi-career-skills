"""Hook closures shared by supervisor and skill agents.

Extracted from ``controller.py`` to keep the controller under the 800-line
hard cap.  Exposes ``build_controller_hooks(...)`` which returns a small
dataclass with the four agent-loop hooks plus a mutable ``agent_ref_box``
the controller populates before each agent invocation.
"""

from __future__ import annotations

from collections.abc import Callable
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

# Keep the hook's early-stop gate at least as strict as the durable completion
# policy. A navigation label or copied snippet must not stop discovery before
# a source-backed JD has been persisted.
_DISCOVERY_NAV_LABEL_TITLES = frozenset(
    {
        "浏览职位", "查看全部", "招聘观察", "申请职位", "职位", "岗位",
        "职位列表", "岗位列表", "热门职位", "招聘职位", "首页", "招聘",
        "投递", "登录", "注册", "联系我们", "关于我们", "返回", "更多",
    }
)


def _real_discovery_candidate(artifact: Any, candidate: Any) -> bool:
    if not isinstance(candidate, dict):
        return False
    title = candidate.get("title")
    if not isinstance(title, str):
        return False
    title = title.strip()
    if len(title) < 2 or title in _DISCOVERY_NAV_LABEL_TITLES:
        return False
    if not any(
        ("一" <= ch <= "鿿") or (ch.isascii() and ch.isalpha())
        for ch in title
    ):
        return False
    body = f"{candidate.get('responsibilities') or ''} {candidate.get('requirements') or ''}".strip()
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
    # EvidenceStore has already attached the structured artifact to its
    # observed source. Some extractors preserve only an apply URL on each
    # candidate; a quality-qualified artifact is therefore sufficient for the
    # early-stop gate, while matching source fields remain preferred when
    # present.
    return (
        any(source == artifact_source for source in candidate_sources)
        or quality == "job_bearing"
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
    # Agents execute business tools sequentially, so one active callback is
    # sufficient; this is deliberately not a cross-delegation concurrency API.
    context_refresh_box: list[Callable[[], None] | None]


def build_controller_hooks(
    *,
    tracker: BudgetTracker,
    guard: ToolCallGuard,
    store: EvidenceStore,
    event_log: EventLogger,
    halt_box: list[tuple[str, str] | None],
    skill_name: str | None = None,
    context_refresh_box: list[Callable[[], None] | None] | None = None,
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
    external_failure_counts: dict[str, int] = {}
    agent_ref_box: list[Any] = [None]
    refresh_box = context_refresh_box if context_refresh_box is not None else [None]
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
            if promoted and refresh_box[0] is not None:
                refresh_box[0]()
            guard.set_artifact_count(len(store.job_bearing_artifacts()))
            succeeded = details.status == "succeeded"
            produced = bool(promoted)
            external_terminate = False

            # A route-exhausted or permanently blocked public source is an
            # external handoff, not a reason to spend the remaining model
            # budget inventing more URLs.  Only short-circuit when no usable
            # evidence exists; if artifacts are already present, the agent
            # still gets a chance to complete downstream work.
            error_code = details.error_code or ""
            if not succeeded and error_code in {
                "anti_bot_challenge",
                "captcha",
                "login_required",
                "needs_manual_review",
            }:
                external_failure_counts["blocked_public_source"] = (
                    external_failure_counts.get("blocked_public_source", 0) + 1
                )
                # A challenge/login/manual-review response is a terminal
                # source hand-off for this route.  Retrying the same public
                # source only increases blocking risk; the supervisor can
                # still finish from any already-promoted deliverable.
                _record_halt(
                    error_code,
                    f"{error_code}: public source requires manual review or a fallback source",
                )
                external_terminate = True
            if not succeeded and error_code in {
                "route_already_consumed",
                "wechat_ocr_disabled",
            }:
                external_failure_counts[error_code] = (
                    external_failure_counts.get(error_code, 0) + 1
                )
                if external_failure_counts[error_code] >= 2:
                    _record_halt(
                        error_code,
                        f"{error_code}: route exhausted without a productive next step",
                    )
                    external_terminate = True
                elif (
                    error_code == "route_already_consumed"
                    and external_failure_counts.get("blocked_public_source", 0) > 0
                ):
                    # A route exhausted after an anti-bot/manual-review
                    # signal is a genuine human hand-off, even when partial
                    # artifacts exist. Do not spend recovery budget replaying
                    # a source that the browser has already identified as
                    # blocked.
                    _record_halt(
                        "anti_bot_challenge",
                        "public source requires manual review after anti-bot challenge",
                    )
                    external_terminate = True

            # Repeated evidence/validation misses with no promotion indicate
            # a deterministic mismatch, not a transient network failure.
            if not succeeded and error_code in {
                "target_evidence_not_found",
                "target_role_mismatch",
                "target_source_mismatch",
                "empty_public_page",
                "public_page_content_insufficient",
                "invalid_tool_input",
            }:
                miss_key = f"miss:{error_code}"
                external_failure_counts[miss_key] = (
                    external_failure_counts.get(miss_key, 0) + 1
                )
                if external_failure_counts[miss_key] >= 3:
                    _record_halt(
                        "no_progress",
                        f"{error_code}: repeated failure without new evidence",
                    )
                    external_terminate = True
            elif produced:
                # New evidence makes a subsequent validation miss meaningful
                # again; do not carry stale failure streaks across progress.
                for key in tuple(external_failure_counts):
                    if key.startswith("miss:"):
                        external_failure_counts.pop(key, None)

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
            if signal == "repeated_tool_failure":
                _record_halt(NO_PROGRESS, f"repeated failure for {name}")
                return {"terminate": True}

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
                    "error_message": getattr(details, "error_message", "") or "",
                    "promoted_artifacts": len(promoted),
                },
            )
            # A durable deliverable is the skill's terminal contract.  Stop
            # the model loop immediately after promotion so it cannot keep
            # browsing, re-extracting the same pages, or retrying a consumed
            # route after the business result is already available.
            if skill_name and _deliverable_ready(skill_name, promoted):
                return {"terminate": True}
            if external_terminate:
                return {"terminate": True}
            return None

        # Case 2: supervisor delegate tool (details is a dict).
        if (
            isinstance(details, dict)
            and "skill" in details
            and "status" in details
        ):
            skill = details.get("skill", "")
            status = details.get("status", "")
            succeeded = status in {"succeeded", "success"}
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
        context_refresh_box=refresh_box,
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


def _deliverable_ready(skill_name: str, artifacts: list[Any]) -> bool:
    """Return whether a newly promoted artifact satisfies a skill contract."""
    expected = {
        "job-discovery": {"public_job_page", "structured_job_details"},
        "job-matching": {"job_matching_report"},
        "resume-tailoring": {"resume_tailoring_brief"},
        "career-planning": {"career_preparation_plan"},
    }.get(skill_name, set())
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
            if any(_real_discovery_candidate(artifact, candidate) for candidate in candidates):
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


__all__ = [
    "ControllerHooks",
    "build_controller_hooks",
]
