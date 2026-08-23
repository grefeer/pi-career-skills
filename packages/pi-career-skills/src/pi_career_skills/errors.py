"""Stable error codes and message redaction for career-skill tools.

Error codes are string constants (never bare strings elsewhere) so the
harness can branch on them safely across skill versions. ``redact_message``
strips credential-like fragments before anything reaches a log, a model
prompt, or an SSE payload.
"""

from __future__ import annotations

import re
from typing import Final

# ---------------------------------------------------------------------------
# Stable error codes (alphabetical order; add new ones at the end)
# ---------------------------------------------------------------------------

ANTI_BOT: Final[str] = "anti_bot"
AUTO_RECOVERY_LIMIT_REACHED: Final[str] = "auto_recovery_limit_reached"
BUDGET_EXHAUSTED: Final[str] = "budget_exhausted"
CANDIDATE_URLS_ALREADY_SUPPLIED: Final[str] = "candidate_urls_already_supplied"
CAPTCHA: Final[str] = "captcha"
COMPLETION_EVIDENCE_UNAVAILABLE: Final[str] = "completion_evidence_unavailable"
CONTRACT_OR_POLICY_ERROR: Final[str] = "contract_or_policy_error"
DELEGATION_SKILL_ALREADY_SUCCEEDED: Final[str] = "delegation_skill_already_succeeded"
DELEGATION_SKILL_NOT_ALLOWED: Final[str] = "delegation_skill_not_allowed"
DUPLICATE_TOOL_CALL: Final[str] = "duplicate_tool_call"
INVALID_MODEL_RESPONSE: Final[str] = "invalid_model_response"
INVALID_TOOL_INPUT: Final[str] = "invalid_tool_input"
INVALID_TOOL_OUTPUT: Final[str] = "invalid_tool_output"
LOGIN_REQUIRED: Final[str] = "login_required"
MODEL_API_KEY_MISSING: Final[str] = "model_api_key_missing"
NEED_USER: Final[str] = "need_user"
NEEDS_MANUAL_REVIEW: Final[str] = "needs_manual_review"
NO_PROGRESS: Final[str] = "no_progress"
NO_PROGRESS_DUPLICATE: Final[str] = "no_progress_duplicate"
PLAN_OSCILLATION_DETECTED: Final[str] = "plan_oscillation_detected"
ROUTE_ALREADY_CONSUMED: Final[str] = "route_already_consumed"
SHEET_CALL_FAILED: Final[str] = "sheet_call_failed"
SHEET_RATE_LIMITED: Final[str] = "sheet_rate_limited"
TARGET_EVIDENCE_NOT_FOUND: Final[str] = "target_evidence_not_found"
TARGET_ROLE_MISMATCH: Final[str] = "target_role_mismatch"
TARGET_SOURCE_MISMATCH: Final[str] = "target_source_mismatch"
TOOL_EXECUTION_FAILED: Final[str] = "tool_execution_failed"
TOOL_SKILL_FORBIDDEN: Final[str] = "tool_skill_forbidden"
UNSAFE_PUBLIC_URL: Final[str] = "unsafe_public_url"
UNKNOWN_TOOL: Final[str] = "unknown_tool"
VERIFICATION_FAILED: Final[str] = "verification_failed"
WALL_CLOCK_BUDGET_EXHAUSTED: Final[str] = "wall_clock_budget_exhausted"
WECHAT_OCR_DISABLED: Final[str] = "wechat_ocr_disabled"

# Error codes that map to status="blocked" rather than "failed".
BLOCKED_ERROR_CODES: frozenset[str] = frozenset(
    {
        LOGIN_REQUIRED,
        CAPTCHA,
        ANTI_BOT,
        NEEDS_MANUAL_REVIEW,
        UNSAFE_PUBLIC_URL,
        WECHAT_OCR_DISABLED,
        SHEET_RATE_LIMITED,
        SHEET_CALL_FAILED,
    }
)

# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class CareerToolError(Exception):
    """Raised by skill tool handlers for deterministic error conditions.

    The adapter reads ``.code`` first when producing a ToolObservation so
    error codes stay stable even if the human-readable message changes.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Message redaction
# ---------------------------------------------------------------------------

# URL scheme + optional userinfo that we want to strip.
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)"
    r"(?P<userinfo>[^@\s/?#]+@)"
    r"(?P<rest>[^\s]+)",
)

# Bearer / token / password / secret patterns (case-insensitive within reason).
_BEARER_RE = re.compile(
    r"\b(?:Bearer|bearer)\s+[A-Za-z0-9_\-.*+/=]{6,}",
)
_TOKEN_KV_RE = re.compile(
    r"(?i)\b((?:api[_-]?key|token|password|secret|auth[_-]?token"
    r"|access[_-]?token|refresh[_-]?token)\s*[:=]\s*)"
    r"[^\s&,;\"'`)]{4,}",
)


def redact_message(msg: str, max_len: int = 500) -> str:
    """Strip credential-like fragments from *msg* and cap length at *max_len*.

    Removes:
    - URL userinfo (``user:pass@host`` → ``***@host``)
    - ``Bearer <token>`` headers
    - ``key=value`` / ``key: value`` pairs for common secret keys

    The result is then truncated to ``max_len`` characters with an ellipsis
    suffix so oversized payloads cannot leak through error messages.
    """

    if not msg:
        return ""

    redacted = _URL_USERINFO_RE.sub(
        lambda m: f"{m.group('scheme')}***@{m.group('rest')}", msg
    )
    redacted = _BEARER_RE.sub("[REDACTED_BEARER]", redacted)
    redacted = _TOKEN_KV_RE.sub(r"\1[REDACTED]", redacted)

    if len(redacted) <= max_len:
        return redacted
    return redacted[:max_len] + "..."


__all__ = [
    # Error codes
    "ANTI_BOT",
    "AUTO_RECOVERY_LIMIT_REACHED",
    "BUDGET_EXHAUSTED",
    "CANDIDATE_URLS_ALREADY_SUPPLIED",
    "CAPTCHA",
    "COMPLETION_EVIDENCE_UNAVAILABLE",
    "CONTRACT_OR_POLICY_ERROR",
    "DELEGATION_SKILL_ALREADY_SUCCEEDED",
    "DELEGATION_SKILL_NOT_ALLOWED",
    "DUPLICATE_TOOL_CALL",
    "INVALID_MODEL_RESPONSE",
    "INVALID_TOOL_INPUT",
    "INVALID_TOOL_OUTPUT",
    "LOGIN_REQUIRED",
    "MODEL_API_KEY_MISSING",
    "NEED_USER",
    "NEEDS_MANUAL_REVIEW",
    "NO_PROGRESS",
    "NO_PROGRESS_DUPLICATE",
    "PLAN_OSCILLATION_DETECTED",
    "ROUTE_ALREADY_CONSUMED",
    "SHEET_CALL_FAILED",
    "SHEET_RATE_LIMITED",
    "TARGET_EVIDENCE_NOT_FOUND",
    "TARGET_ROLE_MISMATCH",
    "TARGET_SOURCE_MISMATCH",
    "TOOL_EXECUTION_FAILED",
    "TOOL_SKILL_FORBIDDEN",
    "UNSAFE_PUBLIC_URL",
    "UNKNOWN_TOOL",
    "VERIFICATION_FAILED",
    "WALL_CLOCK_BUDGET_EXHAUSTED",
    "WECHAT_OCR_DISABLED",
    # Sets
    "BLOCKED_ERROR_CODES",
    # Exception
    "CareerToolError",
    # Functions
    "redact_message",
]
