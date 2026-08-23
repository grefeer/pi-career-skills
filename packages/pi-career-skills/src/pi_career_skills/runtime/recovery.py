"""Bounded auto-recovery policy with explicit human hand-off reasons.

Port of the source ``agent_plugins/policies/recovery.py`` semantics plus
per-skill recovery hints from ``career_skills/*_recovery.py``.  In this
in-memory eval runtime, recovery is orchestrated by the run controller;
this module provides the decision + step-up budget plan.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import (
    ANTI_BOT,
    CAPTCHA,
    CONTRACT_OR_POLICY_ERROR,
    LOGIN_REQUIRED,
    NEEDS_MANUAL_REVIEW,
    PLAN_OSCILLATION_DETECTED,
    TOOL_SKILL_FORBIDDEN,
    UNSAFE_PUBLIC_URL,
    CareerToolError,
)

# ---------------------------------------------------------------------------
# Recovery-reason sets (verbatim from migration plan §7.3).
# ---------------------------------------------------------------------------

#: Reasons that trigger an automatic recovery attempt (up to the limit).
AUTO_RECOVERABLE_REASONS: frozenset[str] = frozenset({
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
})

#: Reasons that are NEVER auto-recovered — always a human hand-off.
#: Codes align with ``errors.py`` constants (migration plan §7.3).
NEVER_AUTO_RECOVER_REASONS: frozenset[str] = frozenset({
    LOGIN_REQUIRED,
    CAPTCHA,
    ANTI_BOT,
    NEEDS_MANUAL_REVIEW,
    UNSAFE_PUBLIC_URL,
    TOOL_SKILL_FORBIDDEN,
    CONTRACT_OR_POLICY_ERROR,
    PLAN_OSCILLATION_DETECTED,
})


@dataclass(frozen=True)
class RecoveryPlan:
    """What changes on the next recovery attempt.

    ``budget_multiplier`` scales the ceilings (clamped to hard caps by the
    caller).  ``reset_streak`` and ``keep_artifacts`` describe state carry-
    over: artifacts are preserved (they are trusted evidence), the stall
    streak resets (fresh chance), messages/context are rebuilt.
    """

    allowed: bool
    reason_code: str
    budget_multiplier: float = 1.0
    reset_stall_streak: bool = True
    keep_artifacts: bool = True
    refresh_wall_clock_window: bool = True


def should_auto_recover(
    error_code: str,
    attempt_index: int,
    max_recoveries: int = 2,
) -> bool:
    """Return True if ``error_code`` is recoverable and budget remains.

    ``attempt_index`` is the number of recovery attempts already made
    (0 = first attempt, no recovery yet).  The default ``max_recoveries=2``
    means up to 3 total attempts (initial + 2 recoveries), matching the
    migration plan §7.1 / §7.3.

    Blocked reasons (login/captcha/anti-bot/unsafe URL) always return
    False — they must go to a human.
    """
    if error_code in NEVER_AUTO_RECOVER_REASONS:
        return False
    if error_code not in AUTO_RECOVERABLE_REASONS:
        return False
    if attempt_index < 0:
        attempt_index = 0
    return attempt_index < max_recoveries


def recovery_plan(attempt_index: int) -> RecoveryPlan:
    """Return the recovery plan for the ``attempt_index``-th recovery.

    ``attempt_index = 0`` → first recovery → 1.5x multiplier.
    ``attempt_index = 1`` → second recovery → 2.0x multiplier.
    ``attempt_index >= 2`` → not allowed → plan with ``allowed=False``
    and ``reason_code="auto_recovery_limit_reached"``.

    The caller (``BudgetTracker.step_up``) applies the multiplier while
    clamping to ``HARD_CAPS``; this function only declares the intended
    multiplier.
    """
    if attempt_index < 0:
        attempt_index = 0
    if attempt_index == 0:
        return RecoveryPlan(
            allowed=True,
            reason_code="retry_recovery",
            budget_multiplier=1.5,
            reset_stall_streak=True,
            keep_artifacts=True,
            refresh_wall_clock_window=True,
        )
    if attempt_index == 1:
        return RecoveryPlan(
            allowed=True,
            reason_code="retry_recovery",
            budget_multiplier=2.0,
            reset_stall_streak=True,
            keep_artifacts=True,
            refresh_wall_clock_window=True,
        )
    return RecoveryPlan(
        allowed=False,
        reason_code="auto_recovery_limit_reached",
        budget_multiplier=1.0,
        reset_stall_streak=False,
        keep_artifacts=True,
        refresh_wall_clock_window=False,
    )


def assert_recovery_allowed(error_code: str, attempt_index: int, max_recoveries: int = 2) -> None:
    """Raise ``CareerToolError(CONTRACT_OR_POLICY_ERROR, ...)`` when a
    recovery is attempted outside policy.

    The controller should call ``should_auto_recover`` first; this is a
    hardening guard at the actual recovery boundary.
    """
    if error_code in NEVER_AUTO_RECOVER_REASONS:
        raise CareerToolError(
            CONTRACT_OR_POLICY_ERROR,
            f"blocked reason {error_code} must not auto-recover",
        )
    if not should_auto_recover(error_code, attempt_index, max_recoveries):
        raise CareerToolError(
            CONTRACT_OR_POLICY_ERROR,
            f"recovery not allowed: {error_code} at attempt {attempt_index}",
        )


__all__ = [
    "AUTO_RECOVERABLE_REASONS",
    "NEVER_AUTO_RECOVER_REASONS",
    "RecoveryPlan",
    "should_auto_recover",
    "recovery_plan",
    "assert_recovery_allowed",
]
