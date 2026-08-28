"""Bounded auto-recovery policy with explicit human hand-off reasons.

Port of the source ``agent_plugins/policies/recovery.py`` semantics plus
per-skill recovery hints from ``career_skills/*_recovery.py``.  In this
in-memory eval runtime, recovery is orchestrated by the run controller;
this module provides its eligibility decision.
"""

from __future__ import annotations

from ..errors import (
    ANTI_BOT,
    CAPTCHA,
    CONTRACT_OR_POLICY_ERROR,
    LOGIN_REQUIRED,
    NEEDS_MANUAL_REVIEW,
    PLAN_OSCILLATION_DETECTED,
    TOOL_SKILL_FORBIDDEN,
    UNSAFE_PUBLIC_URL,
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
__all__ = [
    "AUTO_RECOVERABLE_REASONS",
    "NEVER_AUTO_RECOVER_REASONS",
    "should_auto_recover",
]
