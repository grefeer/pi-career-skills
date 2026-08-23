"""validate-observed-candidates handler (Phase 6 §5 move).

Verbatim move of ``validate_observed_candidates`` and ``_quality_issues`` from
``business/job_discovery/handlers.py`` (previously ported at parity from
``skill/job_discovery/runtime/job_discovery.py``).  The move breaks the
network-handlers import cycle: this module imports ``_find_observed_evidence``
from ``.handlers`` while ``.handlers`` re-exports these functions at the
bottom of the module (``# noqa: E402``), so ``registry.py`` wiring is
untouched.
"""

from __future__ import annotations

import re

from ...context import ToolContext
from .handlers import _find_observed_evidence
from .models import (
    CandidateIssue,
    ValidateObservedCandidatesInput,
    ValidateObservedCandidatesOutput,
)

_MIN_DESCRIPTION_LENGTH = 50
_STALE_YEAR_THRESHOLD = 2024
_JD_KEYWORDS = (
    "岗位",
    "职位",
    "招聘",
    "要求",
    "职责",
    "job",
    "position",
    "requirement",
    "responsibility",
    "qualification",
)
_YEAR_RE = re.compile(r"\b(20[0-9]{2})\b")


def validate_observed_candidates(
    context: ToolContext, payload: ValidateObservedCandidatesInput
) -> ValidateObservedCandidatesOutput:
    """Run the staleness / vagueness / non-JD gates over observed evidence."""
    issues: list[CandidateIssue] = []
    for artifact_id in payload.artifact_ids:
        evidence = _find_observed_evidence(context, artifact_id)
        if evidence is None:
            issues.append(
                CandidateIssue(
                    artifact_id=artifact_id,
                    code="evidence_not_found",
                    detail="no observed evidence with this artifact_id",
                )
            )
            continue
        visible_text = evidence.get("visible_text")
        if not isinstance(visible_text, str) or not visible_text:
            issues.append(
                CandidateIssue(
                    artifact_id=artifact_id,
                    code="evidence_incomplete",
                    detail="evidence has no visible_text",
                )
            )
            continue
        issues.extend(_quality_issues(artifact_id, visible_text))
    return ValidateObservedCandidatesOutput(valid=not issues, issues=issues)


def _quality_issues(artifact_id: str, text: str) -> list[CandidateIssue]:
    """The three evidence-quality gates (validate.py --verify semantics)."""
    issues: list[CandidateIssue] = []
    for year in _YEAR_RE.findall(text):
        if 2000 < int(year) < _STALE_YEAR_THRESHOLD:
            issues.append(
                CandidateIssue(
                    artifact_id=artifact_id,
                    code="stale_year",
                    detail=f"references year {year} (threshold: {_STALE_YEAR_THRESHOLD})",
                )
            )
            break
    stripped = text.strip()
    if len(stripped) < _MIN_DESCRIPTION_LENGTH:
        issues.append(
            CandidateIssue(
                artifact_id=artifact_id,
                code="vague_description",
                detail=f"{len(stripped)} chars (min: {_MIN_DESCRIPTION_LENGTH})",
            )
        )
    lowered = text.lower()
    if len(text) > 100:
        keyword_hits = sum(1 for keyword in _JD_KEYWORDS if keyword in lowered)
        if keyword_hits < 2:
            issues.append(
                CandidateIssue(
                    artifact_id=artifact_id,
                    code="non_jd_text",
                    detail=f"only {keyword_hits} JD keywords found",
                )
            )
    return issues


__all__ = [
    "_MIN_DESCRIPTION_LENGTH",
    "_STALE_YEAR_THRESHOLD",
    "_JD_KEYWORDS",
    "_YEAR_RE",
    "validate_observed_candidates",
    "_quality_issues",
]
