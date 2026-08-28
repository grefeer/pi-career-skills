"""Tests for runtime/recovery.py — auto-recoverable reason sets, step-up
plan, and the should_auto_recover decision logic.
"""

from __future__ import annotations

from pi_career_skills.runtime.recovery import (
    AUTO_RECOVERABLE_REASONS,
    NEVER_AUTO_RECOVER_REASONS,
    should_auto_recover,
)

# ---------------------------------------------------------------------------
# Reason set membership (verbatim from migration plan §7.3)
# ---------------------------------------------------------------------------


def test_auto_recoverable_reasons_match_plan() -> None:
    """The recoverable set must contain exactly the 10 reasons from §7.3."""
    expected = {
        "need_user",
        "verification_failed",
        "no_progress_duplicate",
        "invalid_model_response",
        "wall_clock_budget_exhausted",
        "route_already_consumed",
        "candidate_urls_already_supplied",
        "target_evidence_not_found",
        "target_role_mismatch",
        "target_source_mismatch",
    }
    assert expected == AUTO_RECOVERABLE_REASONS


def test_never_recover_blocked_reasons() -> None:
    """Blocked / blocked reasons must never auto-recover."""
    for reason in [
        "login_required",
        "captcha",
        "anti_bot",
        "needs_manual_review",
        "unsafe_public_url",
        "tool_skill_forbidden",
        "contract_or_policy_error",
        "plan_oscillation_detected",
    ]:
        assert reason in NEVER_AUTO_RECOVER_REASONS
        assert reason not in AUTO_RECOVERABLE_REASONS


# ---------------------------------------------------------------------------
# should_auto_recover
# ---------------------------------------------------------------------------


def test_should_auto_recover_true_within_budget() -> None:
    """Recoverable reason + under limit → True."""
    assert should_auto_recover("need_user", attempt_index=0, max_recoveries=2) is True
    assert should_auto_recover("verification_failed", attempt_index=1, max_recoveries=2) is True


def test_should_auto_recover_false_at_limit() -> None:
    """At the limit → False (no more recoveries allowed)."""
    # max_recoveries=2 means attempts 0, 1 are allowed; attempt 2 is the 3rd.
    assert should_auto_recover("need_user", attempt_index=2, max_recoveries=2) is False


def test_should_auto_recover_blocked_false() -> None:
    """Blocked reasons → False regardless of budget."""
    assert should_auto_recover("captcha", attempt_index=0, max_recoveries=5) is False
    assert should_auto_recover("login_required", attempt_index=0, max_recoveries=5) is False
    assert should_auto_recover("needs_manual_review", attempt_index=0, max_recoveries=5) is False
    assert should_auto_recover("unsafe_public_url", attempt_index=0, max_recoveries=5) is False


def test_should_auto_recover_unknown_reason_false() -> None:
    """Unknown / unclassified reasons → False (fail closed)."""
    assert should_auto_recover("some_random_code", attempt_index=0, max_recoveries=2) is False
    assert should_auto_recover("", attempt_index=0, max_recoveries=2) is False


def test_should_auto_recover_negative_index_clamps() -> None:
    """Negative attempt_index is treated as 0 (initial)."""
    assert should_auto_recover("need_user", attempt_index=-5, max_recoveries=2) is True


# ---------------------------------------------------------------------------
# Step-up clamp via BudgetTracker.step_up integration check
# ---------------------------------------------------------------------------


def test_step_up_clamped_to_hard_caps_via_budget_tracker() -> None:
    """Recovery plan 2.0x must be clamped by the budget tracker to hard caps."""
    from pi_career_skills.runtime.budgets import HARD_CAPS, BudgetLimits, BudgetTracker

    tracker = BudgetTracker(BudgetLimits(
        agent_turns=80,      # 80 * 2 = 160 > 100 → clamp
        initial_tool_calls=250,  # 250 * 2 = 500 > 400 → clamp
        wall_clock_seconds=500,  # 500 * 2 = 1000 > 900 → clamp
    ))
    stepped = tracker.step_up(attempt_index=2)  # 2.0x
    assert stepped.agent_turns == HARD_CAPS["agent_turns"]
    assert stepped.initial_tool_calls == HARD_CAPS["tool_calls"]
    assert stepped.wall_clock_seconds == HARD_CAPS["wall_clock_seconds"]


# ---------------------------------------------------------------------------
# Artifact preservation + stall reset
# ---------------------------------------------------------------------------


def test_recovery_keeps_artifacts_resets_stall() -> None:
    """A new recovery attempt preserves artifacts and resets stall streak."""
    from pi_career_skills.runtime.budgets import ToolCallGuard

    guard = ToolCallGuard()
    # Simulate prior activity: 5 no-progress calls, 2 artifacts.
    for i in range(3):
        guard.note_call(f"t{i}", f"h{i}", succeeded=True, produced_artifact=True)
    for i in range(5):
        guard.note_call(f"x{i}", f"hx{i}", succeeded=True, produced_artifact=False)
    assert guard.stall_streak == 5
    assert guard.artifact_count == 3

    # Recovery: stall streak resets.
    guard.reset_stall_on_delegation()
    assert guard.stall_streak == 0
    # Artifacts preserved.
    assert guard.artifact_count == 3
