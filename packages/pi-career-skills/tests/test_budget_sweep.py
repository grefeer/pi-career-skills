"""Wave 4 budget/error-code consolidation sweep tests.

Verifies:
  - errors.py defines BUDGET_EXHAUSTED / NO_PROGRESS / DELEGATION_SKILL_NOT_ALLOWED
  - runtime/budgets.py raises with the constants (not bare strings)
  - contracts.py no longer exports BudgetLimits / BudgetConsumed
  - state.py / completion.py / recovery.py use CONTRACT_OR_POLICY_ERROR constant
"""

from __future__ import annotations

import importlib

import pytest

from pi_career_skills.errors import (
    BUDGET_EXHAUSTED,
    CONTRACT_OR_POLICY_ERROR,
    DELEGATION_SKILL_NOT_ALLOWED,
    NO_PROGRESS,
    CareerToolError,
)
from pi_career_skills.runtime.budgets import (
    BudgetLimits,
    BudgetTracker,
    ToolCallGuard,
)
from pi_career_skills.runtime.completion import terminal_guard
from pi_career_skills.runtime.state import RunState, RunStatus, transition

# ---------------------------------------------------------------------------
# Constants exist
# ---------------------------------------------------------------------------


def test_new_error_constants_defined() -> None:
    """Three new error codes exist as module-level constants."""
    assert BUDGET_EXHAUSTED == "budget_exhausted"
    assert NO_PROGRESS == "no_progress"
    assert DELEGATION_SKILL_NOT_ALLOWED == "delegation_skill_not_allowed"


# ---------------------------------------------------------------------------
# budgets.py uses constants (not bare strings)
# ---------------------------------------------------------------------------


def test_budgets_turn_exhausted_uses_constant() -> None:
    tracker = BudgetTracker(BudgetLimits(agent_turns=1))
    tracker.consume_turn()
    with pytest.raises(CareerToolError) as exc:
        tracker.consume_turn()
    assert exc.value.code == BUDGET_EXHAUSTED
    assert exc.value.code == "budget_exhausted"  # value identical


def test_budgets_tool_exhausted_uses_constant() -> None:
    tracker = BudgetTracker(BudgetLimits(initial_tool_calls=1))
    tracker.consume_tool_call(artifact_count=0)
    with pytest.raises(CareerToolError) as exc:
        tracker.consume_tool_call(artifact_count=0)
    assert exc.value.code == BUDGET_EXHAUSTED


def test_budgets_model_request_exhausted_uses_constant() -> None:
    tracker = BudgetTracker(BudgetLimits(model_requests=1))
    tracker.consume_model_request(tokens=0)
    with pytest.raises(CareerToolError) as exc:
        tracker.consume_model_request(tokens=0)
    assert exc.value.code == BUDGET_EXHAUSTED


def test_budgets_input_token_exhausted_uses_constant() -> None:
    tracker = BudgetTracker(BudgetLimits(input_tokens=10))
    with pytest.raises(CareerToolError) as exc:
        tracker.consume_input_tokens(11)
    assert exc.value.code == BUDGET_EXHAUSTED


def test_budgets_hard_stall_uses_no_progress_constant() -> None:
    guard = ToolCallGuard()
    for i in range(8):
        guard.note_call(f"tool-{i}", f"h{i}", succeeded=True, produced_artifact=False)
    with pytest.raises(CareerToolError) as exc:
        guard.note_call("tool-8", "h8", succeeded=True, produced_artifact=False)
    assert exc.value.code == NO_PROGRESS
    assert exc.value.code == "no_progress"  # value identical


def test_tool_call_guard_is_duplicate() -> None:
    """ToolCallGuard.is_duplicate returns True for already-succeeded keys."""
    guard = ToolCallGuard()
    assert not guard.is_duplicate("tool-a", "hash1")
    guard.note_call("tool-a", "hash1", succeeded=True, produced_artifact=True)
    assert guard.is_duplicate("tool-a", "hash1")
    assert not guard.is_duplicate("tool-a", "hash2")
    assert not guard.is_duplicate("tool-b", "hash1")


def test_budget_tracker_restore_consumed() -> None:
    """restore_consumed seeds counters; wall_clock resets by default."""
    tracker = BudgetTracker(BudgetLimits())
    from pi_career_skills.runtime.budgets import BudgetConsumed

    prior = BudgetConsumed(
        agent_turns=5,
        tool_calls=10,
        model_requests=7,
        input_tokens=1000,
        wall_clock_seconds=42.5,
        auto_recoveries=1,
    )
    tracker.restore_consumed(prior)
    c = tracker.consumed()
    assert c.agent_turns == 5
    assert c.tool_calls == 10
    assert c.model_requests == 7
    assert c.input_tokens == 1000
    assert c.wall_clock_seconds == 0.0  # reset
    assert c.auto_recoveries == 1


def test_budget_tracker_restore_consumed_preserve_wall_clock() -> None:
    """restore_consumed with reset_wall_clock=False preserves wall clock."""
    tracker = BudgetTracker(BudgetLimits())
    from pi_career_skills.runtime.budgets import BudgetConsumed

    prior = BudgetConsumed(wall_clock_seconds=42.5, agent_turns=3)
    tracker.restore_consumed(prior, reset_wall_clock=False)
    assert tracker.consumed().wall_clock_seconds == 42.5
    assert tracker.consumed().agent_turns == 3


# ---------------------------------------------------------------------------
# state.py / completion.py / recovery.py use CONTRACT_OR_POLICY_ERROR
# ---------------------------------------------------------------------------


def test_state_transition_terminal_uses_constant() -> None:
    state = RunState(run_id="r1", attempt_id="a1", synthetic_user_id="u1")
    state.status = RunStatus.succeeded
    state.terminal = True
    with pytest.raises(CareerToolError) as exc:
        transition(state, RunStatus.failed)
    assert exc.value.code == CONTRACT_OR_POLICY_ERROR


def test_terminal_guard_uses_constant() -> None:
    state = RunState(run_id="r1", attempt_id="a1", synthetic_user_id="u1")
    state.status = RunStatus.succeeded
    state.terminal = True
    with pytest.raises(CareerToolError) as exc:
        terminal_guard(state)
    assert exc.value.code == CONTRACT_OR_POLICY_ERROR


# ---------------------------------------------------------------------------
# contracts.py no longer exports BudgetLimits / BudgetConsumed
# ---------------------------------------------------------------------------


def test_contracts_no_budget_classes() -> None:
    """Importing BudgetLimits/BudgetConsumed from contracts must raise ImportError."""
    with pytest.raises(ImportError):
        from pi_career_skills.contracts import (
            BudgetLimits,  # type: ignore[attr-defined]  # noqa: F401
        )

    with pytest.raises(ImportError):
        from pi_career_skills.contracts import (
            BudgetConsumed,  # type: ignore[attr-defined]  # noqa: F401
        )


def test_contracts_module_has_no_budget_attrs() -> None:
    """The contracts module itself has no BudgetLimits/BudgetConsumed attributes."""
    import pi_career_skills.contracts as contracts_mod

    importlib.reload(contracts_mod)
    assert not hasattr(contracts_mod, "BudgetLimits")
    assert not hasattr(contracts_mod, "BudgetConsumed")
