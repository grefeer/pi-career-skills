"""Tests for runtime/completion.py — per-skill completion gates, run-level
completion policy, terminal guard, and matching fallback.

Uses the real ``EvidenceStore`` from runtime.evidence and populates it via
``ToolObservation`` to exercise the full promotion path.
"""

from __future__ import annotations

import hashlib

import pytest

from pi_career_skills.contracts import ToolObservation
from pi_career_skills.errors import CareerToolError
from pi_career_skills.runtime.completion import (
    RunCompletionPolicy,
    _bounded_summary,
    _is_plausible_job_title,
    discovery_completed,
    matching_completed,
    matching_fallback,
    tailoring_completed,
    terminal_guard,
)
from pi_career_skills.runtime.evidence import EvidenceStore, canonical_json
from pi_career_skills.runtime.state import RunState, RunStatus

# ---------------------------------------------------------------------------
# Pure helpers — verbatim parity with source completion.py
# ---------------------------------------------------------------------------


def test_is_plausible_job_title_rejects_nav_labels() -> None:
    """Navigation/UI labels must never pass as real job titles."""
    for label in ["浏览职位", "查看全部", "招聘", "登录", "首页", "岗位"]:
        assert not _is_plausible_job_title(label), f"{label} should be rejected"


def test_is_plausible_job_title_accepts_real_titles() -> None:
    """Real-looking titles pass."""
    assert _is_plausible_job_title("前端开发工程师")
    assert _is_plausible_job_title("Python Developer")
    assert _is_plausible_job_title("算法工程师")


def test_is_plausible_job_title_rejects_short_or_empty() -> None:
    """Empty, whitespace, or <2-char titles fail."""
    assert not _is_plausible_job_title("")
    assert not _is_plausible_job_title("  ")
    assert not _is_plausible_job_title("A")
    assert not _is_plausible_job_title(None)
    assert not _is_plausible_job_title(123)


def test_bounded_summary_truncates_and_strips() -> None:
    """_bounded_summary: strips, caps at 4000 chars, None for non-str/empty."""
    assert _bounded_summary("  hello  ") == "hello"
    assert _bounded_summary(None) is None
    assert _bounded_summary(123) is None
    assert _bounded_summary("   ") is None
    long = "x" * 5000
    result = _bounded_summary(long)
    assert result is not None
    assert len(result) == 4_000


# ---------------------------------------------------------------------------
# Helper: build a ToolObservation that will promote to an artifact
# ---------------------------------------------------------------------------


def _make_hash(obj: dict) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def _page_obs(quality: str = "jd_complete") -> ToolObservation:
    output = {
        "source_url": "https://example.com/jobs/1",
        "content_hash": _make_hash({"q": quality}),
        "quality": quality,
        "title": "前端开发工程师",
        "responsibilities": "负责 Vue3 组件开发",
        "requirements": "3 年以上经验",
    }
    return ToolObservation(
        tool_name="fetch-public-job-page",
        status="succeeded",
        output=output,
    )


def _details_obs(has_real: bool = True) -> ToolObservation:
    if has_real:
        candidates = [
            {
                "title": "后端开发工程师",
                "responsibilities": "负责微服务架构设计与实现",
                "requirements": "5 年以上 Java 开发经验，熟悉 Spring Boot",
                "source_url": "https://example.com/jobs/2",
                "content_hash": _make_hash({"t": "backend"}),
            }
        ]
    else:
        # Nav-label title with no real body.
        candidates = [
            {
                "title": "浏览职位",
                "responsibilities": "",
                "requirements": "",
                "source_url": "https://example.com/list",
                "content_hash": _make_hash({"t": "nav"}),
            }
        ]
    output = {
        "source_url": "https://example.com/details",
        "content_hash": _make_hash({"c": len(candidates)}),
        "candidates": candidates,
    }
    return ToolObservation(
        tool_name="extract-observed-job-details",
        status="succeeded",
        output=output,
    )


def _matching_obs(
    has_matches: bool = True,
    no_candidate_flag: bool = False,
) -> ToolObservation:
    matches: list[dict] = []
    if has_matches:
        matches.append({
            "rank": 1,
            "source_url": "https://example.com/jobs/1",
            "evidence_excerpt": "要求 3 年以上 Python 经验",
            "score": 85,
        })
    content = {
        "matches": matches,
        "evaluated_candidate_count": 1,
    }
    if no_candidate_flag:
        content["no_candidate_satisfied_constraints"] = True
    output = {
        "source_url": "https://example.com/jobs/1",
        "content_hash": _make_hash(content),
        **content,
    }
    return ToolObservation(
        tool_name="match-observed-jobs",
        status="succeeded",
        output=output,
    )


def _tailoring_obs(has_actions: bool = True) -> ToolObservation:
    content = {
        "target_artifact_id": "art1",
        "source_url": "https://example.com/jobs/1",
        "safe_actions": ["添加 Python 项目经验"] if has_actions else [],
    }
    output = {
        "source_url": "https://example.com/jobs/1",
        "content_hash": _make_hash(content),
        **content,
    }
    return ToolObservation(
        tool_name="build-resume-tailoring-brief",
        status="succeeded",
        output=output,
    )


# ---------------------------------------------------------------------------
# Discovery completion
# ---------------------------------------------------------------------------


def test_discovery_completed_with_jd_complete_page() -> None:
    """A jd_complete public page satisfies discovery."""
    store = EvidenceStore()
    store.add_observation(_page_obs("jd_complete"))
    assert discovery_completed(store) is True


def test_discovery_not_completed_with_list_only() -> None:
    """A list_only page shell does NOT satisfy discovery."""
    store = EvidenceStore()
    store.add_observation(_page_obs("list_only"))
    assert discovery_completed(store) is False


def test_discovery_completed_with_structured_details() -> None:
    """Structured details with a real candidate satisfy discovery."""
    store = EvidenceStore()
    store.add_observation(_details_obs(has_real=True))
    assert discovery_completed(store) is True


def test_discovery_not_completed_with_nav_label_details() -> None:
    """Structured details with only nav-label titles do NOT satisfy discovery."""
    store = EvidenceStore()
    store.add_observation(_details_obs(has_real=False))
    assert discovery_completed(store) is False


def test_discovery_not_completed_with_only_matching_report() -> None:
    """An inherited (chain) matching report alone never completes discovery.

    ``discovery_completed`` is type-scoped to ``public_job_page`` /
    ``structured_job_details``: a run that needs discovery but holds only a
    ``job_matching_report`` artifact has zero discovery evidence.
    """
    store = EvidenceStore()
    store.add_observation(_matching_obs(has_matches=True))
    assert store.job_bearing_artifacts()  # the report IS job-bearing...
    assert discovery_completed(store) is False  # ...but it is not discovery


# ---------------------------------------------------------------------------
# Matching completion
# ---------------------------------------------------------------------------


def test_matching_completed_with_report_and_source() -> None:
    """A report with matches carrying source_url + evidence_excerpt passes."""
    store = EvidenceStore()
    store.add_observation(_matching_obs(has_matches=True))
    assert matching_completed(store) is True


def test_matching_not_completed_empty_no_trace() -> None:
    """Empty matches without no_candidate_satisfied_constraints fails."""
    store = EvidenceStore()
    store.add_observation(_matching_obs(has_matches=False, no_candidate_flag=False))
    assert matching_completed(store) is False


def test_matching_completed_empty_with_evaluation_trace() -> None:
    """Empty matches with explicit no_candidate_satisfied_constraints passes."""
    store = EvidenceStore()
    store.add_observation(_matching_obs(has_matches=False, no_candidate_flag=True))
    assert matching_completed(store) is True


# ---------------------------------------------------------------------------
# Tailoring completion
# ---------------------------------------------------------------------------


def test_tailoring_completed_with_brief_and_actions() -> None:
    """A brief with target_artifact_id, source_url, and safe_actions passes."""
    store = EvidenceStore()
    store.add_observation(_tailoring_obs(has_actions=True))
    assert tailoring_completed(store) is True


def test_tailoring_not_completed_without_actions() -> None:
    """A brief without safe_actions fails."""
    store = EvidenceStore()
    store.add_observation(_tailoring_obs(has_actions=False))
    assert tailoring_completed(store) is False


# ---------------------------------------------------------------------------
# RunCompletionPolicy
# ---------------------------------------------------------------------------


def _make_state(
    status: RunStatus = RunStatus.running,
    needed: set[str] | None = None,
    terminal: bool = False,
    error_code: str | None = None,
) -> RunState:
    state = RunState(
        run_id="r1",
        attempt_id="a1",
        synthetic_user_id="u1",
        status=status,
        needed_skills=needed or set(),
        terminal=terminal,
        error_code=error_code,
    )
    return state


def test_run_succeeded_all_skills_done_summary_present() -> None:
    """All needed skills complete + non-empty summary + resolving ref
    → succeeded (source ANY-semantics: at least one ref must resolve to a
    job-bearing artifact)."""
    store = EvidenceStore()
    page = store.add_observation(_page_obs("jd_complete"))[0]
    store.add_observation(_matching_obs(has_matches=True))

    state = _make_state(
        status=RunStatus.running,
        needed={"job-discovery", "job-matching"},
    )
    state.summary_refs = [{"artifact_id": page.artifact_id}]
    policy = RunCompletionPolicy()
    status, err = policy.evaluate(state, store, summary="找到 1 个匹配职位")
    assert status == "succeeded"
    assert err is None


def test_run_succeeded_mixed_refs_any_semantics() -> None:
    """A non-resolving routing-evidence ref does NOT fail the run when at
    least one other ref resolves (Q045 regression: valid jd_complete pages
    + low-quality shell refs in the store tail)."""
    store = EvidenceStore()
    page = store.add_observation(_page_obs("jd_complete"))[0]

    state = _make_state(
        status=RunStatus.running,
        needed={"job-discovery"},
    )
    state.summary_refs = [
        {"artifact_id": "shell-artifact-1"},  # routing evidence — ignored
        {"artifact_id": page.artifact_id},  # real deliverable — anchors
    ]
    policy = RunCompletionPolicy()
    status, err = policy.evaluate(state, store, summary="找到 1 个职位")
    assert status == "succeeded"
    assert err is None


def test_run_waiting_when_skill_incomplete() -> None:
    """Missing completion on any needed skill → waiting_user."""
    store = EvidenceStore()
    store.add_observation(_page_obs("jd_complete"))  # discovery done only

    state = _make_state(
        status=RunStatus.running,
        needed={"job-discovery", "job-matching"},
    )
    policy = RunCompletionPolicy()
    status, err = policy.evaluate(state, store, summary="done")
    assert status == "waiting_user"
    assert err == "completion_evidence_unavailable"


def test_run_waiting_empty_summary() -> None:
    """All skills done but empty/whitespace summary → waiting_user."""
    store = EvidenceStore()
    store.add_observation(_page_obs("jd_complete"))

    state = _make_state(
        status=RunStatus.running,
        needed={"job-discovery"},
    )
    policy = RunCompletionPolicy()
    status, err = policy.evaluate(state, store, summary="   ")
    assert status == "waiting_user"
    assert err == "completion_evidence_unavailable"


def test_run_waiting_unresolved_summary_refs() -> None:
    """Summary refs pointing at a missing artifact → waiting_user."""
    # The refs-resolution check must have real state to read: this asserts the
    # field exists on RunState so the check is live (would fail if the field
    # regressed out of the dataclass).
    assert "summary_refs" in RunState.__dataclass_fields__
    store = EvidenceStore()
    store.add_observation(_page_obs("jd_complete"))

    state = _make_state(
        status=RunStatus.running,
        needed={"job-discovery"},
    )
    state.summary_refs = [{"artifact_id": "missing-id"}]
    policy = RunCompletionPolicy()
    status, err = policy.evaluate(state, store, summary="找到 1 个职位")
    assert status == "waiting_user"
    assert err == "completion_evidence_unavailable"


def test_run_succeeded_with_resolving_summary_refs() -> None:
    """Summary refs resolving to persisted artifacts → succeeded."""
    store = EvidenceStore()
    artifact = store.add_observation(_page_obs("jd_complete"))[0]

    state = _make_state(
        status=RunStatus.running,
        needed={"job-discovery"},
    )
    state.summary_refs = [{"artifact_id": artifact.artifact_id}]
    policy = RunCompletionPolicy()
    status, err = policy.evaluate(state, store, summary="找到 1 个职位")
    assert status == "succeeded"
    assert err is None


def test_run_waiting_passthrough() -> None:
    """A run already in waiting_user passes through unchanged."""
    store = EvidenceStore()
    store.add_observation(_page_obs("jd_complete"))

    state = _make_state(
        status=RunStatus.waiting_user,
        needed={"job-discovery"},
        error_code="needs_user",
    )
    policy = RunCompletionPolicy()
    status, err = policy.evaluate(state, store, summary="done")
    assert status == "waiting_user"
    assert err == "needs_user"


def test_run_failed_passthrough() -> None:
    """A failed run passes through — completion policy never promotes it."""
    store = EvidenceStore()
    store.add_observation(_page_obs("jd_complete"))

    state = _make_state(
        status=RunStatus.failed,
        needed={"job-discovery"},
        error_code="runtime_error",
    )
    policy = RunCompletionPolicy()
    status, err = policy.evaluate(state, store, summary="done")
    assert status == "failed"
    assert err == "runtime_error"


# ---------------------------------------------------------------------------
# terminal_guard
# ---------------------------------------------------------------------------


def test_terminal_guard_allows_running() -> None:
    """A running run is not blocked by terminal_guard."""
    state = _make_state(status=RunStatus.running)
    terminal_guard(state)  # should not raise


def test_terminal_guard_blocks_succeeded() -> None:
    """A succeeded (terminal) run → contract_or_policy_error."""
    state = _make_state(status=RunStatus.succeeded, terminal=True)
    with pytest.raises(CareerToolError) as exc:
        terminal_guard(state)
    assert exc.value.code == "contract_or_policy_error"


def test_terminal_guard_blocks_failed_status() -> None:
    """Failed status is terminal even if terminal flag not set."""
    state = _make_state(status=RunStatus.failed)
    # Note: our _make_state doesn't auto-set terminal on failed because we
    # construct it directly; the source-of-truth transition() function does.
    # terminal_guard checks BOTH state.terminal AND state.status.
    with pytest.raises(CareerToolError) as exc:
        terminal_guard(state)
    assert exc.value.code == "contract_or_policy_error"


# ---------------------------------------------------------------------------
# matching_fallback
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """Minimal registry stub that counts invoke calls."""

    def __init__(self, succeed: bool = True) -> None:
        self.call_count = 0
        self.succeed = succeed

    def invoke(self, tool_name: str, context: object, params: dict) -> ToolObservation:
        self.call_count += 1
        assert tool_name == "match-observed-jobs"
        if self.succeed:
            return _matching_obs(has_matches=True)
        return ToolObservation(
            tool_name="match-observed-jobs",
            status="failed",
            error_code="tool_execution_failed",
            output=None,
        )


class _FakeContext:
    pass


def test_matching_fallback_calls_once_when_candidates_no_report() -> None:
    """Has structured candidates but no report → exactly one fallback call."""
    store = EvidenceStore()
    store.add_observation(_details_obs(has_real=True))

    from pi_career_skills.runtime.budgets import BudgetTracker

    budget = BudgetTracker()
    guard = None
    registry = _FakeRegistry(succeed=True)

    result = matching_fallback(registry, _FakeContext(), store, budget, guard)
    assert result is not None
    assert registry.call_count == 1
    # Consumed exactly 1 tool call.
    assert budget.consumed().tool_calls == 1


def test_matching_fallback_skips_when_report_exists() -> None:
    """Already has a matching report → no fallback call."""
    store = EvidenceStore()
    store.add_observation(_details_obs(has_real=True))
    store.add_observation(_matching_obs(has_matches=True))

    from pi_career_skills.runtime.budgets import BudgetTracker

    budget = BudgetTracker()
    registry = _FakeRegistry(succeed=True)

    result = matching_fallback(registry, _FakeContext(), store, budget, None)
    assert result is None
    assert registry.call_count == 0
    assert budget.consumed().tool_calls == 0


def test_matching_fallback_skips_when_no_candidates() -> None:
    """No structured candidates → no fallback call."""
    store = EvidenceStore()
    store.add_observation(_page_obs("jd_complete"))  # public page, but no details

    from pi_career_skills.runtime.budgets import BudgetTracker

    budget = BudgetTracker()
    registry = _FakeRegistry(succeed=True)

    result = matching_fallback(registry, _FakeContext(), store, budget, None)
    assert result is None
    assert registry.call_count == 0


def test_matching_failure_does_not_loop() -> None:
    """Fallback call fails → no retry, just returns the failed observation."""
    store = EvidenceStore()
    store.add_observation(_details_obs(has_real=True))

    from pi_career_skills.runtime.budgets import BudgetTracker, ToolCallGuard

    budget = BudgetTracker()
    guard = ToolCallGuard()
    registry = _FakeRegistry(succeed=False)

    result = matching_fallback(registry, _FakeContext(), store, budget, guard)
    assert result is not None
    assert result.status == "failed"
    assert registry.call_count == 1
    assert budget.consumed().tool_calls == 1
