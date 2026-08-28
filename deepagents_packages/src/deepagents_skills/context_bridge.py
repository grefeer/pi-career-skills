"""Bounded live context projection for deepagents skill tools.

The pi reference projection (``RuntimeContextProjection``) uses a
``break``-on-full truncation: the first record that fills the byte budget
stops the projection, so a discovery run that browses many pages keeps
only the first 1-2 evidence records.  That is fine for pi (whose model
extracts after 3-6 pages per the prompt contract, so the pool rarely
overflows), but a deepagents subagent may legitimately browse dozens of
pages before extracting — and then ``extract-observed-job-details-batch``
resolves nothing and the run stalls on ``target_evidence_not_found``.

This bridge therefore overrides the *evidence/candidate* projection with a
**keep-all** strategy: every record stays resolvable by ``artifact_id`` /
``observed:<content_hash>``, and each record's text fields are truncated
so the whole pool fits the same byte budget.  The tool-internal metadata is
never shown to the model, so keeping more (shorter) records costs nothing
in model context and makes downstream lookups robust.
"""

from __future__ import annotations

from typing import Any

from pi_career_skills.runtime.context_projection import RuntimeContextProjection
from pi_career_skills.runtime import context_projection as _cp
from pi_career_skills.runtime.evidence import EvidenceStore

#: Text fields truncated to share the pool budget (evidence / candidates).
_EVIDENCE_TEXT_FIELDS = ("visible_text",)
_CANDIDATE_TEXT_FIELDS = ("requirements", "responsibilities", "page_text_prefix")


def _fit_all_records(
    records: list[dict[str, Any]],
    budget: int,
    text_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Keep *every* record within ``budget`` by capping shared text fields.

    Unlike ``context_projection._bounded_records`` (which stops at the first
    record that no longer fits), this returns all records — ids always stay
    resolvable — and binary-searches a common per-field character cap so the
    aggregate JSON stays within the budget.  Records whose non-text metadata
    alone exceeds the budget are dropped defensively.
    """
    if not records:
        return records

    def _sized(records: list[dict[str, Any]], cap: int) -> int:
        total = 2  # ``[]``
        for record in records:
            candidate = dict(record)
            for field in text_fields:
                value = candidate.get(field)
                if isinstance(value, str) and len(value) > cap:
                    candidate[field] = value[:cap]
            size = _cp._json_size(candidate)
            if size is None:
                return None
            total += 1 + size
        return total

    # Fast path: everything fits at full length.
    if (total := _sized(records, 2**31)) is not None and total <= budget:
        return records

    longest = 0
    for record in records:
        for field in text_fields:
            value = record.get(field)
            if isinstance(value, str):
                longest = max(longest, len(value))

    lo, hi, best = 0, longest, None
    while lo <= hi:
        mid = (lo + hi) // 2
        total = _sized(records, mid)
        if total is not None and total <= budget:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    cap = best if best is not None else 0

    out: list[dict[str, Any]] = []
    for record in records:
        candidate = dict(record)
        for field in text_fields:
            value = candidate.get(field)
            if isinstance(value, str) and len(value) > cap:
                candidate[field] = value[:cap]
        if _cp._json_size(candidate) is not None:
            out.append(candidate)
    return out


class _KeepAllProjection(RuntimeContextProjection):
    """pi projection with keep-all evidence/candidate pools."""

    def refresh(self, metadata: dict[str, Any], store: EvidenceStore) -> None:
        metadata["observed_public_evidence"] = _keep_all_evidence(
            self._inherited_evidence, store
        )
        metadata["structured_job_candidates"] = _keep_all_candidates(
            self._inherited_candidates, store
        )


def _keep_all_evidence(
    inherited: list[dict[str, Any]], store: EvidenceStore
) -> list[dict[str, Any]]:
    """All public-page records, text-truncated to share the byte budget."""
    records: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for artifact in _cp._live_then_seeded_artifacts(store, "public_job_page"):
        content = artifact.content or {}
        _cp._add_evidence_record(
            records,
            order,
            {
                "artifact_id": content.get("artifact_id") or artifact.artifact_id,
                "artifact_aliases": content.get("artifact_aliases"),
                "store_artifact_id": artifact.artifact_id,
                "artifact_type": "public_job_page",
                "source_url": artifact.source_url,
                "content_hash": artifact.content_hash,
                "quality": artifact.quality or content.get("quality"),
                "visible_text": content.get("visible_text"),
            },
        )
    for item in inherited:
        _cp._add_evidence_record(records, order, item)
    return _fit_all_records(
        [records[key] for key in order], _cp._MAX_EVIDENCE_BYTES, _EVIDENCE_TEXT_FIELDS
    )


def _keep_all_candidates(
    inherited: list[dict[str, Any]], store: EvidenceStore
) -> list[dict[str, Any]]:
    """All structured candidates, text-truncated to share the budget."""
    records: dict[tuple[str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str]] = []
    for artifact in _cp._live_then_seeded_artifacts(store, "structured_job_details"):
        content = artifact.content or {}
        candidates = content.get("candidates") or content.get("details") or []
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            _cp._add_candidate_record(
                records,
                order,
                {
                    **candidate,
                    "artifact_id": candidate.get("artifact_id")
                    or content.get("artifact_id")
                    or artifact.artifact_id,
                    "store_artifact_id": artifact.artifact_id,
                    "artifact_aliases": _cp._joined_lists(
                        candidate.get("artifact_aliases"), content.get("artifact_aliases")
                    ),
                    "source_artifact_id": candidate.get("source_artifact_id")
                    or content.get("source_artifact_id")
                    or artifact.artifact_id,
                    "source_artifact_aliases": _cp._joined_lists(
                        candidate.get("source_artifact_aliases"),
                        content.get("source_artifact_aliases"),
                    ),
                    "source_url": candidate.get("source_url") or artifact.source_url,
                    "content_hash": candidate.get("content_hash")
                    or artifact.content_hash,
                    "candidate_id": candidate.get("candidate_id") or candidate.get("id"),
                },
            )
    for item in inherited:
        _cp._add_candidate_record(records, order, item)
    fitted = _fit_all_records(
        [records[key] for key in order],
        _cp._MAX_CANDIDATE_BYTES,
        _CANDIDATE_TEXT_FIELDS,
    )
    return fitted[:_cp._MAX_CANDIDATES]


class ContextProjectionBridge:
    """Adapt the shared pi evidence projection to a deepagents run."""

    def __init__(self, private_context: Any = None) -> None:
        self._projection = _KeepAllProjection(private_context)

    def metadata(
        self,
        store: EvidenceStore,
        *,
        task_goal: str | None = None,
    ) -> dict[str, Any]:
        """Return a bounded, current metadata snapshot for one tool call."""
        metadata = self._projection.initial_metadata(store)
        if task_goal is not None:
            metadata["task_goal"] = task_goal
        metadata.setdefault("enforce_public_request_governor", True)
        metadata.setdefault("public_request_interval_seconds", 2.5)
        metadata.setdefault("public_page_cache_ttl_seconds", 6 * 60 * 60)
        metadata.setdefault("public_block_cooldown_seconds", 30 * 60)
        return metadata


__all__ = ["ContextProjectionBridge"]
