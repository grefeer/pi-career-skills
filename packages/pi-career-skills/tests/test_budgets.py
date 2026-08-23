"""Tests for runtime/budgets.py — hard budgets, dynamic tool quota, duplicate
detection, and stall handling.

Key-point coverage: default limits, hard caps, overrun behavior,
effective_tool_cap edge cases, step-up multipliers, duplicate call
semantics, soft/hard stall thresholds, and the rule that failed calls
still consume budget.
"""

from __future__ import annotations

import pytest

from pi_career_skills.errors import CareerToolError
from pi_career_skills.runtime.budgets import (
    HARD_CAPS,
    BudgetLimits,
    BudgetTracker,
    ToolCallGuard,
)

# ---------------------------------------------------------------------------
# Default limits and hard caps
# ---------------------------------------------------------------------------


def test_default_limits_match_plan() -> None:
    """Default BudgetLimits must match migration plan §7.1 non-chain column."""
    limits = BudgetLimits()
    assert limits.agent_turns == 100
    assert limits.initial_tool_calls == 200
    assert limits.model_requests == 500
    assert limits.input_tokens == 2_000_000
    assert limits.wall_clock_seconds == 600
    assert limits.auto_recoveries == 2


def test_hard_caps_match_plan() -> None:
    """HARD_CAPS must match migration plan §7.1 hard-upper column."""
    assert HARD_CAPS["agent_turns"] == 100
    assert HARD_CAPS["tool_calls"] == 400
    assert HARD_CAPS["model_requests"] == 500
    assert HARD_CAPS["input_tokens"] == 2_000_000
    assert HARD_CAPS["wall_clock_seconds"] == 900
    assert HARD_CAPS["auto_recoveries"] == 2


def test_limits_clamped_to_hard_caps() -> None:
    """Values above HARD_CAPS are silently clamped at construction."""
    limits = BudgetLimits(
        agent_turns=999,
        initial_tool_calls=9999,
        model_requests=9999,
        input_tokens=9_999_999,
        wall_clock_seconds=9999,
        auto_recoveries=99,
    )
    assert limits.agent_turns == HARD_CAPS["agent_turns"]
    assert limits.initial_tool_calls == HARD_CAPS["tool_calls"]
    assert limits.model_requests == HARD_CAPS["model_requests"]
    assert limits.input_tokens == HARD_CAPS["input_tokens"]
    assert limits.wall_clock_seconds == HARD_CAPS["wall_clock_seconds"]
    assert limits.auto_recoveries == HARD_CAPS["auto_recoveries"]


# ---------------------------------------------------------------------------
# BudgetTracker — consumption and exhaustion
# ---------------------------------------------------------------------------


def test_consume_turn_exhausted_raises() -> None:
    """Agent-turns overrun raises budget_exhausted and does not charge."""
    tracker = BudgetTracker(BudgetLimits(agent_turns=2))
    tracker.consume_turn()
    tracker.consume_turn()
    assert tracker.consumed().agent_turns == 2
    with pytest.raises(CareerToolError) as exc:
        tracker.consume_turn()
    assert exc.value.code == "budget_exhausted"
    # Hard ceiling not breached.
    assert tracker.consumed().agent_turns == 2


def test_consume_tool_call_uses_dynamic_cap() -> None:
    """Tool-call limit grows with artifact count up to the hard cap."""
    tracker = BudgetTracker(BudgetLimits(initial_tool_calls=5))
    # 0 artifacts → cap = 5
    for _ in range(5):
        tracker.consume_tool_call(artifact_count=0)
    assert tracker.consumed().tool_calls == 5
    with pytest.raises(CareerToolError) as exc:
        tracker.consume_tool_call(artifact_count=0)
    assert exc.value.code == "budget_exhausted"


def test_consume_tool_call_grows_with_artifacts() -> None:
    """More artifacts → higher tool cap."""
    tracker = BudgetTracker(BudgetLimits(initial_tool_calls=5))
    # 5 artifacts → cap = min(400, 5 + 5*8) = 45
    for _ in range(45):
        tracker.consume_tool_call(artifact_count=5)
    assert tracker.consumed().tool_calls == 45
    with pytest.raises(CareerToolError):
        tracker.consume_tool_call(artifact_count=5)


def test_consume_model_request_with_tokens() -> None:
    """Model requests and input tokens both have hard ceilings."""
    tracker = BudgetTracker(
        BudgetLimits(model_requests=2, input_tokens=100)
    )
    tracker.consume_model_request(tokens=40)
    tracker.consume_model_request(tokens=50)
    assert tracker.consumed().model_requests == 2
    assert tracker.consumed().input_tokens == 90
    # Third request — still under token cap but over model-request cap.
    with pytest.raises(CareerToolError) as exc:
        tracker.consume_model_request(tokens=5)
    assert exc.value.code == "budget_exhausted"
    assert tracker.consumed().model_requests == 2
    assert tracker.consumed().input_tokens == 90


def test_input_token_overrun_rejects_request() -> None:
    """A request that would exceed the token ceiling is not charged at all."""
    tracker = BudgetTracker(BudgetLimits(model_requests=10, input_tokens=50))
    tracker.consume_model_request(tokens=30)
    assert tracker.consumed().input_tokens == 30
    # 30 + 25 = 55 > 50 — must raise and not charge.
    with pytest.raises(CareerToolError) as exc:
        tracker.consume_model_request(tokens=25)
    assert exc.value.code == "budget_exhausted"
    assert tracker.consumed().model_requests == 1
    assert tracker.consumed().input_tokens == 30


# ---------------------------------------------------------------------------
# effective_tool_cap
# ---------------------------------------------------------------------------


def test_effective_tool_cap_zero_artifacts() -> None:
    """0 artifacts → initial_tool_calls (200 default)."""
    tracker = BudgetTracker()
    assert tracker.effective_tool_cap(0) == 200


def test_effective_tool_cap_five_artifacts() -> None:
    """5 artifacts → 200 + 5*8 = 240."""
    tracker = BudgetTracker()
    assert tracker.effective_tool_cap(5) == 240


def test_effective_tool_cap_clamps_at_hard_max() -> None:
    """>25 artifacts → clamped at 400 (hard cap)."""
    tracker = BudgetTracker()
    # 26 artifacts → 200 + 26*8 = 408 → clamped to 400
    assert tracker.effective_tool_cap(26) == 400
    assert tracker.effective_tool_cap(100) == 400


# ---------------------------------------------------------------------------
# step-up
# ---------------------------------------------------------------------------


def test_step_up_first_recovery_one_and_a_half_x() -> None:
    """First recovery → 1.5x multiplier, clamped to hard caps."""
    tracker = BudgetTracker(BudgetLimits(
        agent_turns=60,
        initial_tool_calls=100,
        model_requests=200,
        input_tokens=1_000_000,
        wall_clock_seconds=400,
    ))
    stepped = tracker.step_up(attempt_index=1)
    assert stepped.agent_turns == 90        # 60 * 1.5
    assert stepped.initial_tool_calls == 150  # 100 * 1.5
    assert stepped.model_requests == 300   # 200 * 1.5
    assert stepped.input_tokens == 1_500_000  # 1M * 1.5
    assert stepped.wall_clock_seconds == 600  # 400 * 1.5


def test_step_up_second_recovery_two_x() -> None:
    """Second recovery → 2.0x multiplier."""
    tracker = BudgetTracker(BudgetLimits(
        initial_tool_calls=100,
        wall_clock_seconds=300,
    ))
    stepped = tracker.step_up(attempt_index=2)
    assert stepped.initial_tool_calls == 200
    assert stepped.wall_clock_seconds == 600


def test_step_up_clamped_to_hard_caps() -> None:
    """Step-up never exceeds HARD_CAPS, even when 2.0x would."""
    tracker = BudgetTracker(BudgetLimits(
        agent_turns=80,     # 80 * 2 = 160 > 100 → clamp to 100
        initial_tool_calls=300,  # 300 * 2 = 600 > 400 → clamp
    ))
    stepped = tracker.step_up(attempt_index=2)
    assert stepped.agent_turns == HARD_CAPS["agent_turns"]
    assert stepped.initial_tool_calls == HARD_CAPS["tool_calls"]


def test_step_up_zero_attempt_no_change() -> None:
    """Attempt 0 (initial) → 1.0x (no change)."""
    tracker = BudgetTracker(BudgetLimits(
        initial_tool_calls=200, wall_clock_seconds=600,
    ))
    stepped = tracker.step_up(attempt_index=0)
    assert stepped.initial_tool_calls == 200
    assert stepped.wall_clock_seconds == 600


# ---------------------------------------------------------------------------
# ToolCallGuard — duplicate detection
# ---------------------------------------------------------------------------


def test_duplicate_same_name_and_hash_after_success() -> None:
    """Re-calling a succeeded tool with same params → duplicate_tool_call."""
    guard = ToolCallGuard()
    result = guard.note_call("tool-a", "hash1", succeeded=True, produced_artifact=True)
    assert result is None
    assert guard.artifact_count == 1

    result = guard.note_call("tool-a", "hash1", succeeded=True, produced_artifact=False)
    assert result == "duplicate_tool_call"
    # Duplicate does NOT advance the artifact count.
    assert guard.artifact_count == 1


def test_failed_call_not_duplicate() -> None:
    """A failed call followed by a retry with same params is NOT a duplicate."""
    guard = ToolCallGuard()
    # First call fails.
    guard.note_call("tool-a", "hash1", succeeded=False, produced_artifact=False)
    # Retry — should NOT be flagged as duplicate.
    result = guard.note_call("tool-a", "hash1", succeeded=True, produced_artifact=False)
    assert result != "duplicate_tool_call"
    # But stall streak increments because no artifact.
    assert guard.stall_streak == 2


def test_different_params_not_duplicate() -> None:
    """Different params with same tool name → not duplicate."""
    guard = ToolCallGuard()
    guard.note_call("tool-a", "hash1", succeeded=True, produced_artifact=True)
    result = guard.note_call("tool-a", "hash2", succeeded=True, produced_artifact=False)
    assert result != "duplicate_tool_call"


def test_duplicate_does_not_consume_stall_streak() -> None:
    """Duplicate calls do not advance (or reset) the stall streak."""
    guard = ToolCallGuard()
    # Build up a streak of 3.
    for i in range(3):
        guard.note_call(f"tool-{i}", f"h{i}", succeeded=True, produced_artifact=False)
    assert guard.stall_streak == 3
    # A duplicate — streak unchanged.
    guard.note_call("tool-0", "h0", succeeded=True, produced_artifact=False)
    assert guard.stall_streak == 3


# ---------------------------------------------------------------------------
# ToolCallGuard — stall handling
# ---------------------------------------------------------------------------


def test_soft_stop_at_six_no_progress() -> None:
    """6 consecutive no-progress calls → soft_stop signal."""
    guard = ToolCallGuard()
    signal: str | None = None
    for i in range(6):
        signal = guard.note_call(
            f"tool-{i}", f"h{i}", succeeded=True, produced_artifact=False,
        )
    assert signal == "soft_stop"
    assert guard.stall_streak == 6


def test_hard_stop_at_nine_no_progress() -> None:
    """9 consecutive no-progress calls → hard stop (raises CareerToolError)."""
    guard = ToolCallGuard()
    for i in range(8):
        guard.note_call(f"tool-{i}", f"h{i}", succeeded=True, produced_artifact=False)
    assert guard.stall_streak == 8
    with pytest.raises(CareerToolError) as exc:
        guard.note_call("tool-8", "h8", succeeded=True, produced_artifact=False)
    assert exc.value.code == "no_progress"


def test_artifact_resets_stall_streak() -> None:
    """Producing a new artifact resets the stall streak to 0."""
    guard = ToolCallGuard()
    for i in range(5):
        guard.note_call(f"tool-{i}", f"h{i}", succeeded=True, produced_artifact=False)
    assert guard.stall_streak == 5
    guard.note_call("tool-5", "h5", succeeded=True, produced_artifact=True)
    assert guard.stall_streak == 0
    assert guard.artifact_count == 1


def test_reset_stall_on_delegation() -> None:
    """Delegation boundary resets stall streak but preserves artifact count."""
    guard = ToolCallGuard()
    for i in range(4):
        guard.note_call(f"tool-{i}", f"h{i}", succeeded=True, produced_artifact=False)
    guard.set_artifact_count(3)
    assert guard.stall_streak == 4
    guard.reset_stall_on_delegation()
    assert guard.stall_streak == 0
    assert guard.artifact_count == 3


# ---------------------------------------------------------------------------
# Failed calls consume budget
# ---------------------------------------------------------------------------


def test_failed_calls_consume_tool_budget() -> None:
    """Per migration plan §7.1: failed-but-executed calls still cost budget."""
    tracker = BudgetTracker(BudgetLimits(initial_tool_calls=3))
    # Simulate 3 failed calls.
    for _ in range(3):
        tracker.consume_tool_call(artifact_count=0)
    assert tracker.consumed().tool_calls == 3
    # Budget exhausted.
    with pytest.raises(CareerToolError):
        tracker.consume_tool_call(artifact_count=0)


# ---------------------------------------------------------------------------
# Wall clock tracking
# ---------------------------------------------------------------------------


def test_wall_clock_not_exhausted_before_attempt() -> None:
    """Before any attempt starts, wall-clock consumed is 0."""
    tracker = BudgetTracker(BudgetLimits(wall_clock_seconds=600))
    assert tracker.consumed().wall_clock_seconds == 0.0
    assert not tracker.wall_clock_exhausted()


def test_wall_clock_finish_accumulates() -> None:
    """After mark_attempt_finished, wall-clock consumed increases."""
    tracker = BudgetTracker(BudgetLimits(wall_clock_seconds=600))
    tracker.mark_attempt_started()
    # Spend a tiny bit of time (we just verify it's non-decreasing).
    tracker.mark_attempt_finished()
    consumed = tracker.consumed().wall_clock_seconds
    assert consumed >= 0.0
    # Not yet exhausted after trivial elapsed.
    assert not tracker.wall_clock_exhausted()
