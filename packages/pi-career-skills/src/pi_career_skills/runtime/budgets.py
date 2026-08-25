"""Run-level in-memory budgets, dynamic tool quota, duplicate detection, and stall handling.

Port of the trusted-kernel budget semantics from the source project's
``agent_kernel/budgets.py``, ``agent_kernel/tool_progress.py``, and
``agent_kernel/tool_calls.py``.  State is per-run in memory; there is no
MySQL reservation ledger in this eval runtime.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..errors import BUDGET_EXHAUSTED, NO_PROGRESS, CareerToolError

# ---------------------------------------------------------------------------
# Hard caps (schema ceiling — no run may ever exceed these).
# ---------------------------------------------------------------------------

HARD_CAPS: dict[str, int] = {
    "agent_turns": 100,
    "tool_calls": 400,
    "model_requests": 500,
    "input_tokens": 2_000_000,
    "wall_clock_seconds": 900,
    "auto_recoveries": 2,
}

#: Extra tool-call headroom granted per durable artifact persisted.
PER_ARTIFACT_HEADROOM = 8

#: Soft stall threshold — the model gets a wrap-up warning.
SOFT_STALL_THRESHOLD = 6

#: Hard stall threshold — the run must stop before looping.
HARD_STALL_THRESHOLD = 9


@dataclass(frozen=True)
class BudgetLimits:
    """Configured budget ceilings for one run (or one chain link).

    Defaults match the non-chain eval baseline from the migration plan §7.1.
    Values are clamped against ``HARD_CAPS`` at construction.
    """

    agent_turns: int = 100
    initial_tool_calls: int = 200
    model_requests: int = 500
    input_tokens: int = 2_000_000
    wall_clock_seconds: int = 600
    auto_recoveries: int = 2

    def __post_init__(self) -> None:
        if self.agent_turns > HARD_CAPS["agent_turns"]:
            object.__setattr__(self, "agent_turns", HARD_CAPS["agent_turns"])
        if self.initial_tool_calls > HARD_CAPS["tool_calls"]:
            object.__setattr__(self, "initial_tool_calls", HARD_CAPS["tool_calls"])
        if self.model_requests > HARD_CAPS["model_requests"]:
            object.__setattr__(self, "model_requests", HARD_CAPS["model_requests"])
        if self.input_tokens > HARD_CAPS["input_tokens"]:
            object.__setattr__(self, "input_tokens", HARD_CAPS["input_tokens"])
        if self.wall_clock_seconds > HARD_CAPS["wall_clock_seconds"]:
            object.__setattr__(self, "wall_clock_seconds", HARD_CAPS["wall_clock_seconds"])
        if self.auto_recoveries > HARD_CAPS["auto_recoveries"]:
            object.__setattr__(self, "auto_recoveries", HARD_CAPS["auto_recoveries"])


@dataclass
class BudgetConsumed:
    """Amount of each budget category already spent."""

    agent_turns: int = 0
    tool_calls: int = 0
    model_requests: int = 0
    input_tokens: int = 0
    wall_clock_seconds: float = 0.0
    auto_recoveries: int = 0


class BudgetTracker:
    """Track per-run budget consumption and enforce hard ceilings.

    Each ``consume_*`` call checks the limit first and raises
    ``CareerToolError(BUDGET_EXHAUSTED, ...)`` when the ceiling is
    reached — the caller does *not* get charged for the refused attempt,
    matching the source kernel's "reserve then finalize" semantics for the
    purpose of the hard ceiling.  Failed-but-executed calls *are* charged
    (source: migration plan §7.1 note).
    """

    def __init__(self, limits: BudgetLimits | None = None) -> None:
        self._limits = limits or BudgetLimits()
        self._consumed = BudgetConsumed()
        self._attempt_start: float | None = None

    # -- limits / caps -----------------------------------------------------

    @property
    def limits(self) -> BudgetLimits:
        return self._limits

    def effective_tool_cap(self, artifact_count: int) -> int:
        """Run-level tool-call ceiling that grows with durable evidence.

        ``min(hard_max, initial + artifact_count * 8)`` — see migration
        plan §7.2.  Only new trusted artifacts count as progress.
        """
        grown = self._limits.initial_tool_calls + artifact_count * PER_ARTIFACT_HEADROOM
        return min(HARD_CAPS["tool_calls"], grown)

    def step_up(self, attempt_index: int) -> BudgetLimits:
        """Return stepped-up limits for recovery attempt ``attempt_index``.

        First recovery: 1.5x; second recovery: 2.0x.  All values clamped
        to ``HARD_CAPS``.  The original tracker is *not* mutated; the
        caller installs the returned limits on a fresh attempt.
        """
        if attempt_index <= 0:
            multiplier = 1.0
        elif attempt_index == 1:
            multiplier = 1.5
        else:
            multiplier = 2.0
        base = self._limits
        return BudgetLimits(
            agent_turns=min(HARD_CAPS["agent_turns"], int(base.agent_turns * multiplier)),
            initial_tool_calls=min(HARD_CAPS["tool_calls"], int(base.initial_tool_calls * multiplier)),
            model_requests=min(HARD_CAPS["model_requests"], int(base.model_requests * multiplier)),
            input_tokens=min(HARD_CAPS["input_tokens"], int(base.input_tokens * multiplier)),
            wall_clock_seconds=min(HARD_CAPS["wall_clock_seconds"], int(base.wall_clock_seconds * multiplier)),
            auto_recoveries=base.auto_recoveries,
        )

    # -- consumption -------------------------------------------------------

    def consume_turn(self) -> None:
        if self._consumed.agent_turns >= self._limits.agent_turns:
            raise CareerToolError(BUDGET_EXHAUSTED, "agent_turns budget exhausted")
        self._consumed.agent_turns += 1

    def consume_tool_call(self, artifact_count: int = 0) -> None:
        cap = self.effective_tool_cap(artifact_count)
        if self._consumed.tool_calls >= cap:
            raise CareerToolError(BUDGET_EXHAUSTED, "tool_calls budget exhausted")
        self._consumed.tool_calls += 1

    def consume_model_request(self, tokens: int = 0) -> None:
        if self._consumed.model_requests >= self._limits.model_requests:
            raise CareerToolError(BUDGET_EXHAUSTED, "model_requests budget exhausted")
        if tokens < 0:
            tokens = 0
        if self._consumed.input_tokens + tokens > self._limits.input_tokens:
            raise CareerToolError(BUDGET_EXHAUSTED, "input_tokens budget exhausted")
        self._consumed.model_requests += 1
        self._consumed.input_tokens += tokens

    def consume_input_tokens(self, n: int) -> None:
        if n < 0:
            n = 0
        if self._consumed.input_tokens + n > self._limits.input_tokens:
            raise CareerToolError(BUDGET_EXHAUSTED, "input_tokens budget exhausted")
        self._consumed.input_tokens += n

    # -- wall clock --------------------------------------------------------

    def mark_attempt_started(self) -> None:
        """Record the start of a new attempt for wall-clock tracking."""
        self._attempt_start = time.monotonic()

    def mark_attempt_finished(self) -> None:
        """Finalize wall-clock consumption for the current attempt."""
        if self._attempt_start is None:
            return
        elapsed = time.monotonic() - self._attempt_start
        if elapsed < 0:
            elapsed = 0.0
        self._consumed.wall_clock_seconds += elapsed
        self._attempt_start = None

    def wall_clock_exhausted(self) -> bool:
        """True when cumulative wall-clock time exceeds the configured limit."""
        return self.remaining_wall_clock_seconds() <= 0.0

    def remaining_wall_clock_seconds(self) -> float:
        """Return wall-clock budget including the currently active attempt."""
        elapsed = self._consumed.wall_clock_seconds
        if self._attempt_start is not None:
            elapsed += max(0.0, time.monotonic() - self._attempt_start)
        return max(0.0, self._limits.wall_clock_seconds - elapsed)

    # -- snapshots ---------------------------------------------------------

    def consumed(self) -> BudgetConsumed:
        """Return a copy of current consumption."""
        return BudgetConsumed(
            agent_turns=self._consumed.agent_turns,
            tool_calls=self._consumed.tool_calls,
            model_requests=self._consumed.model_requests,
            input_tokens=self._consumed.input_tokens,
            wall_clock_seconds=self._consumed.wall_clock_seconds,
            auto_recoveries=self._consumed.auto_recoveries,
        )

    def remaining(self) -> BudgetConsumed:
        """Return remaining budget (tool_calls uses dynamic cap at 0 artifacts)."""
        return BudgetConsumed(
            agent_turns=max(0, self._limits.agent_turns - self._consumed.agent_turns),
            tool_calls=max(0, self.effective_tool_cap(0) - self._consumed.tool_calls),
            model_requests=max(0, self._limits.model_requests - self._consumed.model_requests),
            input_tokens=max(0, self._limits.input_tokens - self._consumed.input_tokens),
            wall_clock_seconds=max(0.0, self._limits.wall_clock_seconds - self._consumed.wall_clock_seconds),
            auto_recoveries=max(0, self._limits.auto_recoveries - self._consumed.auto_recoveries),
        )

    def record_recovery(self) -> None:
        """Increment the auto-recovery counter.

        Raises ``BUDGET_EXHAUSTED`` if ``auto_recoveries`` is already at
        the limit; the caller should use ``should_auto_recover`` from the
        recovery module first.
        """
        if self._consumed.auto_recoveries >= self._limits.auto_recoveries:
            raise CareerToolError(BUDGET_EXHAUSTED, "auto_recovery_limit_reached")
        self._consumed.auto_recoveries += 1

    def restore_consumed(self, consumed: BudgetConsumed, *, reset_wall_clock: bool = True) -> None:
        """Seed counters from a prior attempt.

        Wall clock is zeroed (window refresh on recovery per plan §7.3)
        unless ``reset_wall_clock=False``.  Turn / tool / model / token
        counters are cumulative and never reset.
        """
        self._consumed.agent_turns = consumed.agent_turns
        self._consumed.tool_calls = consumed.tool_calls
        self._consumed.model_requests = consumed.model_requests
        self._consumed.input_tokens = consumed.input_tokens
        self._consumed.auto_recoveries = consumed.auto_recoveries
        if reset_wall_clock:
            self._consumed.wall_clock_seconds = 0.0
        else:
            self._consumed.wall_clock_seconds = consumed.wall_clock_seconds

    def child(self, limits: BudgetLimits) -> DelegationBudgetTracker:
        """Create a per-delegation tracker bounded by this run tracker."""
        return DelegationBudgetTracker(self, limits)


class DelegationBudgetTracker:
    """Child budget that charges both its local quota and the run ceiling."""

    def __init__(self, parent: BudgetTracker, limits: BudgetLimits) -> None:
        self._parent = parent
        self._local = BudgetTracker(limits)

    @property
    def limits(self) -> BudgetLimits:
        return self._local.limits

    def consume_turn(self) -> None:
        self._local.consume_turn()
        try:
            self._parent.consume_turn()
        except Exception:
            self._local._consumed.agent_turns -= 1
            raise

    def consume_tool_call(self, artifact_count: int = 0) -> None:
        self._local.consume_tool_call(artifact_count)
        try:
            self._parent.consume_tool_call(artifact_count)
        except Exception:
            self._local._consumed.tool_calls -= 1
            raise

    def consume_model_request(self, tokens: int = 0) -> None:
        self._local.consume_model_request(tokens)
        try:
            self._parent.consume_model_request(tokens)
        except Exception:
            self._local._consumed.model_requests -= 1
            self._local._consumed.input_tokens = max(0, self._local._consumed.input_tokens - max(0, tokens))
            raise

    def consume_input_tokens(self, n: int) -> None:
        self._local.consume_input_tokens(n)
        try:
            self._parent.consume_input_tokens(n)
        except Exception:
            self._local._consumed.input_tokens = max(0, self._local._consumed.input_tokens - max(0, n))
            raise

    def wall_clock_exhausted(self) -> bool:
        return self._local.wall_clock_exhausted() or self._parent.wall_clock_exhausted()

    def remaining_wall_clock_seconds(self) -> float:
        """Return the tighter local/parent wall-clock remainder."""
        return min(
            self._local.remaining_wall_clock_seconds(),
            self._parent.remaining_wall_clock_seconds(),
        )

    def mark_attempt_started(self) -> None:
        self._local.mark_attempt_started()

    def mark_attempt_finished(self) -> None:
        self._local.mark_attempt_finished()

    def consumed(self) -> BudgetConsumed:
        return self._local.consumed()

    def remaining(self) -> BudgetConsumed:
        return self._local.remaining()


class ToolCallGuard:
    """Duplicate-call detection and stall-progress tracking per run.

    Mirrors the source ``ToolProgressTracker`` plus the duplicate-admission
    logic from ``KernelToolCallBoundary.before()``.

    A call is *duplicate* when the same ``(tool_name, params_hash)`` was
    previously recorded as **succeeded** — it returns ``duplicate_tool_call``
    and does *not* consume budget.  A failed call does not count as a
    duplicate; retrying a failed call is allowed (and charges budget).

    ``stall_streak`` increments on every call where ``produced_artifact`` is
    False and resets to 0 on progress.  At 6 → soft stop signal; at 9 → hard
    stop raises ``CareerToolError(NO_PROGRESS, ...)``.
    """

    def __init__(self) -> None:
        # last successful call key for duplicate detection (run-wide)
        self._last_succeeded: tuple[str, str] | None = None
        # also keep run-wide set of all succeeded keys for cross-call dedup
        self._succeeded_keys: set[tuple[str, str]] = set()
        self._failed_counts: dict[tuple[str, str], int] = {}
        self._stall_streak: int = 0
        self._artifact_count: int = 0

    @property
    def stall_streak(self) -> int:
        return self._stall_streak

    @property
    def artifact_count(self) -> int:
        return self._artifact_count

    def set_artifact_count(self, count: int) -> None:
        """Update the durable-artifact total (monotonic — never decreases)."""
        if count < 0:
            count = 0
        if count > self._artifact_count:
            self._artifact_count = count

    def reset_stall_on_delegation(self) -> None:
        """Reset the no-progress streak at a fresh delegation boundary.

        Artifact count (and the dynamic tool cap it drives) is preserved;
        only the streak is cleared.  See migration plan §7.2.
        """
        self._stall_streak = 0

    def is_duplicate(self, tool_name: str, params_hash: str) -> bool:
        """True when this (tool_name, params_hash) already succeeded."""
        return (tool_name, params_hash) in self._succeeded_keys

    def note_call(
        self,
        tool_name: str,
        params_hash: str,
        succeeded: bool,
        produced_artifact: bool,
    ) -> str | None:
        """Record one tool call and return any stop signal.

        Returns:
          ``"duplicate_tool_call"`` — consecutive (or run-wide) identical
              successful call; budget must NOT be consumed.
          ``"soft_stop"`` — stall streak has reached the soft threshold;
              the model should wrap up with current evidence.  The call
              *does* execute and consume budget.
          ``None`` — proceed normally.

        Raises ``CareerToolError(NO_PROGRESS, ...)`` when the hard stall
        threshold is reached — the caller must stop the run.
        """
        key = (tool_name, params_hash)

        # Duplicate check: same name + params as a previously succeeded call.
        if succeeded and key in self._succeeded_keys:
            # Was already succeeded before; this is a re-invocation of the
            # same successful call → duplicate, no budget, no streak change.
            return "duplicate_tool_call"

        if not succeeded:
            self._failed_counts[key] = self._failed_counts.get(key, 0) + 1
            if self._failed_counts[key] >= 3:
                return "repeated_tool_failure"

        # Not a duplicate.  Execute and update progress state.
        if produced_artifact:
            self._stall_streak = 0
            self._artifact_count += 1
        else:
            self._stall_streak += 1

        if succeeded:
            self._succeeded_keys.add(key)
            self._last_succeeded = key

        # Hard stall — raise; soft stall — return signal.
        if self._stall_streak >= HARD_STALL_THRESHOLD:
            raise CareerToolError(
                NO_PROGRESS,
                f"hard stall after {self._stall_streak} consecutive no-progress calls",
            )
        if self._stall_streak >= SOFT_STALL_THRESHOLD:
            return "soft_stop"
        return None


__all__ = [
    "HARD_CAPS",
    "PER_ARTIFACT_HEADROOM",
    "SOFT_STALL_THRESHOLD",
    "HARD_STALL_THRESHOLD",
    "BudgetLimits",
    "BudgetConsumed",
    "BudgetTracker",
    "DelegationBudgetTracker",
    "ToolCallGuard",
]
