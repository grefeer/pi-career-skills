"""Deterministic completion policy for the run-level harness.

Port of ``agent_plugins/policies/completion.py`` from the source project.
Constants and the ``_is_quality_job_bearing`` / ``_has_real_structured_candidate``
/ ``_is_plausible_job_title`` / ``_bounded_summary`` pure functions are
ported verbatim.  The database dependency is replaced by ``EvidenceStore``.

Also includes per-skill completion gates (migration plan §6.5), the run-level
``RunCompletionPolicy``, ``terminal_guard``, and the matching fallback
deterministic one-shot (§6.6).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import CareerToolError

# ---------------------------------------------------------------------------
# Constants — VERBATIM from source completion.py.
# ---------------------------------------------------------------------------

#: Artifact types that constitute a job-bearing deliverable for completion.
#: A `job_search_results` shell is routing evidence (it may be empty/blocked),
#: so it alone must never satisfy the completion gate.
_JOB_BEARING_ARTIFACT_TYPES = frozenset(
    {
        "public_job_page",
        "structured_job_details",
        "job_matching_report",
        "resume_tailoring_brief",
    }
)

#: Titles that are navigation/UI labels rather than real job titles.  A
#: structured candidate whose title is one of these (or otherwise implausible)
#: does not carry a usable JD and must not anchor a successful deliverable.
_NAV_LABEL_TITLES = frozenset(
    {
        "浏览职位",
        "查看全部",
        "招聘观察",
        "申请职位",
        "职位",
        "岗位",
        "职位列表",
        "岗位列表",
        "热门职位",
        "招聘职位",
        "首页",
        "招聘",
        "投递",
        "登录",
        "注册",
        "联系我们",
        "关于我们",
        "返回",
        "更多",
    }
)

#: Minimum combined responsibilities/requirements length for a usable JD body.
_MIN_JD_BODY_CHARS = 20


# ---------------------------------------------------------------------------
# Pure helpers — VERBATIM from source completion.py.
# ---------------------------------------------------------------------------


def _is_quality_job_bearing(artifact: Any) -> bool:
    """True when the artifact's payload carries a real, complete JD."""
    if artifact.artifact_type not in _JOB_BEARING_ARTIFACT_TYPES:
        return False
    content = artifact.content or {}
    if artifact.artifact_type == "public_job_page":
        return content.get("quality") == "jd_complete"
    if artifact.artifact_type == "structured_job_details":
        return _has_real_structured_candidate(content)
    # Derived deliverables (matching report / resume brief) only materialize
    # after real input; keep them type-based.
    return True


def _has_real_structured_candidate(content: Mapping[str, Any]) -> bool:
    candidates = content.get("candidates") or content.get("details") or []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        if not _is_plausible_job_title(candidate.get("title")):
            continue
        body = (
            f"{candidate.get('responsibilities') or ''} "
            f"{candidate.get('requirements') or ''}"
        ).strip()
        if len(body) >= _MIN_JD_BODY_CHARS:
            return True
    return False


def _is_plausible_job_title(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    title = value.strip()
    if len(title) < 2:
        return False
    if title in _NAV_LABEL_TITLES:
        return False
    # A real title carries at least one CJK character or ASCII letter.
    return any(
        ("一" <= ch <= "鿿") or ch.isascii() and ch.isalpha()
        for ch in title
    )


def _bounded_summary(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:4_000] or None


# ---------------------------------------------------------------------------
# Per-skill completion gates (migration plan §6.5).
# ---------------------------------------------------------------------------


def discovery_completed(store: Any) -> bool:
    """True when job-discovery has produced at least one quality deliverable.

    Per migration plan §6.5:
      - a ``public_job_page`` with ``quality == "jd_complete"``, OR
      - a ``structured_job_details`` that contains at least one plausible
        title + a real responsibilities/requirements body (≥20 chars).
    """
    return any(
        _is_quality_job_bearing(artifact)
        for artifact in store.job_bearing_artifacts()
        if artifact.artifact_type in {"public_job_page", "structured_job_details"}
    )


def matching_completed(store: Any) -> bool:
    """True when job-matching has produced a usable report.

    Per migration plan §6.5:
      - a ``job_matching_report`` artifact exists, AND
      - either:
        * non-empty matches with at least one entry carrying
          ``source_url`` / ``evidence_excerpt``, OR
        * empty matches with explicit ``no_candidate_satisfied_constraints``
          (the run genuinely evaluated and found none).
    """
    for artifact in store.job_bearing_artifacts():
        if artifact.artifact_type != "job_matching_report":
            continue
        content = artifact.content or {}
        matches = content.get("matches") or []
        if matches:
            for m in matches:
                if not isinstance(m, Mapping):
                    continue
                if m.get("source_url") and m.get("evidence_excerpt"):
                    return True
            return False
        # Empty matches — must have explicit evaluation trace.  The handler
        # emits ``no_match_reason="no_candidate_satisfied_constraints"`` (the
        # literal is the value, not the key); accept both spellings.
        if content.get("no_candidate_satisfied_constraints") or (
            content.get("no_match_reason")
            == "no_candidate_satisfied_constraints"
        ):
            return True
    return False


def tailoring_completed(store: Any) -> bool:
    """True when resume-tailoring has produced a usable brief.

    Per migration plan §6.5:
      - a ``resume_tailoring_brief`` artifact exists, AND
      - it carries ``target_artifact_id``, ``source_url``, and non-empty
        ``safe_actions``.
    """
    for artifact in store.job_bearing_artifacts():
        if artifact.artifact_type != "resume_tailoring_brief":
            continue
        content = artifact.content or {}
        if (
            content.get("target_artifact_id")
            and content.get("source_url")
            and content.get("safe_actions")
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Run-level completion policy
# ---------------------------------------------------------------------------


#: Terminal run statuses — once reached, no new tool calls are admitted.
TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})


class RunCompletionPolicy:
    """Evaluate the overall run completion against ``RunState`` + evidence.

    A run ``succeeded`` only when every needed skill has completed, the
    summary is non-empty, and all artifact references in the summary/refs
    resolve to persisted artifacts in ``EvidenceStore``.

    Otherwise the run stays in ``waiting_user`` with
    ``completion_evidence_unavailable`` — matching the source
    ``CompletionVerdict`` semantics: waiting/failed always win over a
    model-claimed success.
    """

    SKILL_CHECKERS: dict[str, Any] = {
        "job-discovery": discovery_completed,
        "job-matching": matching_completed,
        "resume-tailoring": tailoring_completed,
    }

    def evaluate(
        self,
        state: Any,
        store: Any,
        summary: str | None = None,
    ) -> tuple[str, str | None]:
        """Return ``(status, error_code)`` for the current run state.

        Args:
            state: a ``RunState``-like object with ``status``, ``terminal``,
                ``error_code``, and ``needed_skills`` attributes.
            store: an ``EvidenceStore``-like object with
                ``job_bearing_artifacts()`` and ``refs()`` methods.
            summary: the model-claimed summary text, if any.

        Returns:
            ``(status, error_code)`` — see module docstring for rules.
        """
        # Waiting or failed states pass through unchanged.
        if state.status == "waiting_user":
            return ("waiting_user", state.error_code or "needs_user")
        if state.status == "failed":
            return ("failed", state.error_code or "runtime_error")
        if state.status == "cancelled":
            return ("cancelled", state.error_code or "cancelled")

        # Compute whether every needed skill has completed.
        needed = getattr(state, "needed_skills", None) or []
        if not needed:
            # No skills requested — nothing to succeed on.
            return ("waiting_user", "completion_evidence_unavailable")

        for skill in needed:
            checker = self.SKILL_CHECKERS.get(skill)
            if checker is None:
                return ("waiting_user", "completion_evidence_unavailable")
            if not checker(store):
                return ("waiting_user", "completion_evidence_unavailable")

        # Summary must be present and bounded-non-empty.
        bounded = _bounded_summary(summary)
        if not bounded:
            return ("waiting_user", "completion_evidence_unavailable")

        # Summary refs — if the state declares any refs, they must resolve.
        refs = getattr(state, "summary_refs", None) or []
        if refs:
            persisted_ids = {a.artifact_id for a in store.job_bearing_artifacts()}
            persisted_hashes = {a.content_hash for a in store.job_bearing_artifacts()}
            for ref in refs:
                if isinstance(ref, Mapping):
                    ref_id = ref.get("artifact_id")
                    ref_hash = ref.get("content_hash")
                else:
                    ref_id = ref
                    ref_hash = None
                if ref_id and ref_id in persisted_ids:
                    continue
                if ref_hash and ref_hash in persisted_hashes:
                    continue
                return ("waiting_user", "completion_evidence_unavailable")

        return ("succeeded", None)


def terminal_guard(state: Any) -> None:
    """Reject any new call once the run has reached a terminal state.

    Raises ``CareerToolError("contract_or_policy_error", ...)`` when
    ``state.terminal`` is True or ``state.status`` is in
    ``TERMINAL_STATUSES``.
    """
    if getattr(state, "terminal", False):
        raise CareerToolError(
            "contract_or_policy_error",
            "run is already terminal — no new calls admitted",
        )
    if getattr(state, "status", None) in TERMINAL_STATUSES:
        raise CareerToolError(
            "contract_or_policy_error",
            f"run status {state.status} is terminal — no new calls admitted",
        )


# ---------------------------------------------------------------------------
# Matching fallback (migration plan §6.6).
# ---------------------------------------------------------------------------


def matching_fallback(
    registry: Any,
    context: Any,
    store: Any,
    budget_tracker: Any,
    tool_guard: Any | None = None,
) -> Any:
    """Deterministic one-shot fallback for job-matching.

    Per migration plan §6.6: when a run needs matching, structured
    candidates exist, but no ``job_matching_report`` has been produced
    yet, the controller calls ``match-observed-jobs`` exactly once before
    finalization.  The call goes through the same adapter, consumes normal
    budget, and failures do not loop.

    Args:
        registry: tool registry with an ``invoke(tool_name, context, params)``
            method or equivalent adapter.
        context: ``ToolContext`` for this run.
        store: ``EvidenceStore`` — used to check whether a report already
            exists and whether structured candidates are present.
        budget_tracker: ``BudgetTracker`` — the call consumes 1 tool call
            and 1 model request token is NOT added (it is a deterministic
            handler, not a model turn).
        tool_guard: optional ``ToolCallGuard`` — records the call for
            stall/duplicate tracking.

    Returns:
        The ``ToolObservation`` from the single invocation, or ``None`` if
        the fallback was not needed (report already exists, or no candidates
        to match).
    """
    # Already have a report → skip.
    if matching_completed(store):
        return None

    # No structured candidates → nothing to match → skip.
    has_candidates = False
    for artifact in store.job_bearing_artifacts():
        if artifact.artifact_type == "structured_job_details":
            content = artifact.content or {}
            candidates = content.get("candidates") or content.get("details") or []
            if candidates:
                has_candidates = True
                break
    if not has_candidates:
        return None

    # Consume one tool call against the dynamic cap.
    artifact_count = _count_artifacts(store)
    budget_tracker.consume_tool_call(artifact_count=artifact_count)

    # Invoke exactly once.
    try:
        observation = registry.invoke("match-observed-jobs", context, {})
    except Exception:  # pragma: no cover - hardened by adapter
        # The adapter normally converts exceptions to failed observations;
        # this is a safety net so a raw exception never loops.
        if tool_guard is not None:
            tool_guard.note_call(
                "match-observed-jobs",
                "matching_fallback",
                succeeded=False,
                produced_artifact=False,
            )
        raise

    # Update stall guard with the outcome.  A succeeded ``match-observed-jobs``
    # call always persists at least one report artifact (even an empty-match
    # report is persisted), so ``status == "succeeded"`` is the produced signal.
    if tool_guard is not None:
        produced = getattr(observation, "status", None) == "succeeded"
        tool_guard.note_call(
            "match-observed-jobs",
            "matching_fallback",
            succeeded=getattr(observation, "status", None) == "succeeded",
            produced_artifact=produced,
        )

    return observation


def _count_artifacts(store: Any) -> int:
    """Count job-bearing artifacts for the dynamic tool-cap formula."""
    count = 0
    for _ in store.job_bearing_artifacts():
        count += 1
    return count


__all__ = [
    "TERMINAL_STATUSES",
    "RunCompletionPolicy",
    "discovery_completed",
    "matching_completed",
    "tailoring_completed",
    "terminal_guard",
    "matching_fallback",
]
