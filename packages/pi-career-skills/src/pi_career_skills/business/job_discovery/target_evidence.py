"""Resolve model-visible JD pointers to tool-side evidence."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit


def _normalized_url(value: object) -> str | None:
    """Normalize only harmless URL presentation differences for lookup."""
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value.strip()
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def resolve_target_evidence(
    raw_evidence: object,
    structured_candidates: object,
    target_id: str,
) -> dict[str, Any] | None:
    """Resolve an observed artifact or extracted candidate pointer.

    The model may select a raw page ``artifact_id``, an extracted
    ``candidate_id``/``artifact_id``/``source_artifact_id``, or a matching
    result's source URL. Full JD text is reconstructed only at the tool
    boundary; the decision context continues to carry identifiers only.
    """
    target = _find_raw_target(raw_evidence, target_id)
    if target is not None:
        visible_text = target.get("visible_text")
        if isinstance(visible_text, str) and visible_text.strip():
            # A complete observed page is authoritative for the raw artifact;
            # do not replace it with a same-id structured candidate.
            return target
    target_source_url = target.get("source_url") if target is not None else None
    candidate = _find_structured_candidate(
        structured_candidates,
        target_id,
        target_source_url=target_source_url,
    )
    if candidate is None:
        return target

    candidate_text = _candidate_text(candidate)
    if not candidate_text:
        return target
    resolved = dict(target) if target is not None else {}
    resolved.update(
        {
            # Keep the canonical structured artifact identity separate from
            # the model-selected reference.  Writing candidate_id back into
            # artifact_id made downstream extract/tailoring lookups ambiguous.
            "artifact_id": candidate.get("artifact_id") or target_id,
            "selected_evidence_ref": target_id,
            "candidate_id": candidate.get("candidate_id"),
            "source_artifact_id": candidate.get("source_artifact_id"),
            "source_url": candidate.get("page_source_url") or candidate.get("source_url"),
            "apply_url": candidate.get("apply_url") or candidate.get("source_url"),
            "title": candidate.get("title"),
            "visible_text": candidate_text,
        }
    )
    if isinstance(candidate.get("content_hash"), str):
        resolved["content_hash"] = candidate["content_hash"]
    return resolved


def _find_raw_target(raw_evidence: object, target_id: str) -> dict[str, Any] | None:
    if not isinstance(raw_evidence, list):
        return None
    for item in raw_evidence:
        if isinstance(item, dict) and (
            item.get("artifact_id") == target_id
            or _normalized_url(item.get("source_url")) == _normalized_url(target_id)
            or f"observed:{item.get('content_hash')}" == target_id
        ):
            return item
    return None


def _find_structured_candidate(
    candidates: object,
    target_id: str,
    *,
    target_source_url: object,
) -> dict[str, Any] | None:
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if (
            target_id.strip() == candidate.get("candidate_id")
            or target_id == candidate.get("artifact_id")
            or target_id == candidate.get("source_artifact_id")
            or _normalized_url(target_id) == _normalized_url(candidate.get("source_url"))
            or (
                isinstance(target_source_url, str)
                and target_source_url
                and _normalized_url(target_source_url)
                in {
                    _normalized_url(candidate.get("source_url")),
                    _normalized_url(candidate.get("page_source_url")),
                }
            )
        ):
            return candidate
    return None


def _candidate_text(candidate: dict[str, Any]) -> str | None:
    full_text = candidate.get("full_text")
    if isinstance(full_text, str) and full_text.strip():
        return full_text
    sections = [
        candidate.get("title"),
        candidate.get("company_name"),
        candidate.get("responsibilities"),
        candidate.get("requirements"),
    ]
    text = "\n".join(
        section for section in sections if isinstance(section, str) and section.strip()
    )
    return text or None
