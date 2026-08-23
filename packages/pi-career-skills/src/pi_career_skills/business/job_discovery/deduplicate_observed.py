"""deduplicate-observed-jobs handler (Phase 6 §5 move).

Verbatim move of the dedup block (``deduplicate_observed_jobs`` plus the
identity-key helpers and constants) from ``business/job_discovery/handlers.py``.
The pi adaptation noted in §5 is kept: ``_evidence_identity_keys`` catches
``CareerToolError`` (the source catches ``PublicJobFetchError``; on the pi
side the extract handler raises the pi error type, which is the superset).

Cycle note: this module imports ``_find_observed_evidence``,
``_parse_adapter_evidence`` and ``extract_observed_job_details`` from
``.handlers`` while ``.handlers`` re-exports these functions at the bottom of
the module (``# noqa: E402``), so ``registry.py`` wiring is untouched.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

from ...context import ToolContext
from ...errors import CareerToolError
from .handlers import (
    _find_observed_evidence,
    _parse_adapter_evidence,
    extract_observed_job_details,
)
from .models import (
    DeduplicatedRemoval,
    DeduplicateObservedJobsInput,
    DeduplicateObservedJobsOutput,
    ExtractedJobDetails,
    ExtractObservedJobDetailsInput,
)

_INVISIBLE_CHARS = "​‌‍‎‏﻿　\t"
_ASCII_PUNCT = ",.:;!?()[]\"'<>/\\-~"
_CJK_PUNCT = (
    "【】「」『』《》〈〉"
    "〔〕，。、；：！？（）"
)
_DELETE_TABLE = str.maketrans("", "", _ASCII_PUNCT + _CJK_PUNCT)
_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_QUALIFIER_RE = re.compile(
    r"(?:（[^（）]*）|\([^()]*\)|【[^【】]*】)\s*$"
)


def deduplicate_observed_jobs(
    context: ToolContext, payload: DeduplicateObservedJobsInput
) -> DeduplicateObservedJobsOutput:
    """Dedupe observed artifacts by canonical identity, preserving run order."""
    kept: list[str] = []
    removed: list[DeduplicatedRemoval] = []
    seen: dict[str, str] = {}
    for artifact_id in payload.artifact_ids:
        evidence = _find_observed_evidence(context, artifact_id)
        if evidence is None:
            removed.append(
                DeduplicatedRemoval(
                    artifact_id=artifact_id,
                    reason="evidence_not_found",
                    detail="no observed evidence with this artifact_id",
                )
            )
            continue
        visible_text = evidence.get("visible_text")
        if not isinstance(visible_text, str) or not visible_text:
            removed.append(
                DeduplicatedRemoval(
                    artifact_id=artifact_id,
                    reason="evidence_incomplete",
                    detail="evidence has no visible_text",
                )
            )
            continue
        keys = _evidence_identity_keys(context, artifact_id, evidence)
        if not keys:
            kept.append(artifact_id)
            continue
        collision = next((seen[key] for key in keys if key in seen), None)
        if collision is not None:
            removed.append(
                DeduplicatedRemoval(
                    artifact_id=artifact_id,
                    reason="duplicate_identity",
                    detail=f"shares identity with kept artifact {collision}",
                )
            )
            continue
        kept.append(artifact_id)
        for key in keys:
            seen.setdefault(key, artifact_id)
    return DeduplicateObservedJobsOutput(kept=kept, removed=removed)


def _evidence_identity_keys(
    context: ToolContext, artifact_id: str, evidence: dict[str, object]
) -> tuple[str, ...]:
    """All canonical identity keys one artifact claims, in priority order."""
    records = _parse_adapter_evidence(str(evidence["visible_text"]))
    if records is not None:
        keys = [key for record in records for key in _record_identity_keys(record)]
        return tuple(dict.fromkeys(keys))
    try:
        output = extract_observed_job_details(
            context, ExtractObservedJobDetailsInput(artifact_id=artifact_id)
        )
    except CareerToolError:
        return ()
    keys = [key for candidate in output.candidates for key in _detail_identity_keys(candidate)]
    return tuple(dict.fromkeys(keys))


def _record_identity_keys(record: dict[str, object]) -> tuple[str, ...]:
    """Identity keys for one normalized adapter record."""
    job_id = record.get("job_id")
    if isinstance(job_id, str) and job_id:
        return (f"job_id:{job_id}",)
    apply_url = record.get("apply_url")
    url_key = _url_identity(apply_url if isinstance(apply_url, str) else None)
    location = record.get("location")
    locations = [location] if isinstance(location, str) and location else []
    title = record.get("title")
    title_key = _title_identity(
        title if isinstance(title, str) else None, locations, []
    )
    return tuple(key for key in (url_key, title_key) if key)


def _detail_identity_keys(detail: ExtractedJobDetails) -> tuple[str, ...]:
    """Identity keys for one extracted JD candidate (page-text path)."""
    url_key = _url_identity(detail.apply_url)
    if url_key:
        return (url_key,)
    title_key = _title_identity(
        detail.title, detail.locations, detail.recruitment_types
    )
    return (title_key,) if title_key else ()


def _url_identity(apply_url: str | None) -> str:
    normalized = _normalize_apply_url(apply_url)
    return f"url:{normalized}" if normalized else ""


def _normalize_apply_url(url: str | None) -> str:
    """Normalize an apply URL for identity comparison."""
    if not url:
        return ""
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    if parsed.scheme not in {"http", "https"}:
        return url.strip().lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.rstrip("/") or "/"
    # Drop common tracking params.
    query_parts = [
        f"{k}={v}"
        for k, vs in parse_query_safe(parsed.query).items()
        for v in vs
        if k.lower() not in {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "source"}
    ]
    query = "&".join(sorted(query_parts))
    normalized = f"{parsed.scheme.lower()}://{host}{path}"
    if query:
        normalized += f"?{query}"
    return normalized


def parse_query_safe(query: str) -> dict[str, list[str]]:
    """Safely parse a query string; returns {} on error."""
    if not query:
        return {}
    try:
        from urllib.parse import parse_qs
        return parse_qs(query)
    except ValueError:
        return {}


def _title_identity(
    title: str | None,
    locations: list[str],
    recruitment_types: list[str],
) -> str:
    """Normalized-title identity, scoped by location/recruitment type."""
    if not title or not title.strip():
        return ""
    normalized = _normalize_title(title)
    if not normalized:
        return ""
    loc_key = "+".join(sorted(locations)) if locations else ""
    rt_key = "+".join(sorted(recruitment_types)) if recruitment_types else ""
    return f"title:{normalized}|loc:{loc_key}|rt:{rt_key}"


def _normalize_title(title: str) -> str:
    """Aggressively normalize a title for identity comparison."""
    text = title
    # Strip invisible characters.
    for ch in _INVISIBLE_CHARS:
        text = text.replace(ch, "")
    # Strip trailing qualifiers like (校招) / （北京）.
    text = _TRAILING_QUALIFIER_RE.sub("", text)
    # Normalize unicode.
    text = unicodedata.normalize("NFKC", text)
    # Remove punctuation.
    text = text.translate(_DELETE_TABLE)
    # Collapse whitespace.
    text = _WHITESPACE_RE.sub("", text)
    return text.lower()


__all__ = [
    "_INVISIBLE_CHARS",
    "_ASCII_PUNCT",
    "_CJK_PUNCT",
    "_DELETE_TABLE",
    "_WHITESPACE_RE",
    "_TRAILING_QUALIFIER_RE",
    "deduplicate_observed_jobs",
    "_evidence_identity_keys",
    "_record_identity_keys",
    "_detail_identity_keys",
    "_url_identity",
    "_normalize_apply_url",
    "parse_query_safe",
    "_title_identity",
    "_normalize_title",
]
