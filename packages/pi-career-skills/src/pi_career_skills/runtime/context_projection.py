"""Bounded, live-first EvidenceStore projections for tool contexts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from ..contracts import ToolObservation
from .evidence import EvidenceStore

_MAX_EVIDENCE_BYTES = 24_000
_MAX_CANDIDATE_BYTES = 32_000
_MAX_CANDIDATES = 12
_MAX_ALIASES = 8
_MAX_ID_CHARS = 256
_MAX_URL_CHARS = 2_048
_MAX_HASH_CHARS = 128
_MAX_TEXT_CHARS = 4_000
_MAX_PREFIX_CHARS = 2_000


def safe_private_context(private_context: Any) -> dict[str, Any]:
    """Return a copy of a mapping context, or an empty fail-closed context."""
    if not isinstance(private_context, Mapping):
        return {}
    try:
        copied = deepcopy(private_context)
        return {
            key: value
            for key, value in copied.items()
            if isinstance(key, str)
        }
    except Exception:  # noqa: BLE001 - malformed inherited context is ignored
        return {}


class RuntimeContextProjection:
    """Refresh a tool context from an immutable inherited snapshot and store."""

    def __init__(self, private_context: Any = None) -> None:
        self._base_metadata = safe_private_context(private_context)
        self._inherited_evidence = _dict_items(
            self._base_metadata.get("observed_public_evidence")
        )
        self._inherited_candidates = _dict_items(
            self._base_metadata.get("structured_job_candidates")
        )

    def initial_metadata(self, store: EvidenceStore) -> dict[str, Any]:
        """Return an isolated metadata copy with a current evidence view."""
        try:
            metadata = deepcopy(self._base_metadata)
        except Exception:  # noqa: BLE001 - retain only safe projection keys
            metadata = {}
        self.refresh(metadata, store)
        return metadata

    def refresh(self, metadata: dict[str, Any], store: EvidenceStore) -> None:
        """Refresh evidence keys only; confirmed facts and other context remain."""
        metadata["observed_public_evidence"] = _project_evidence(
            self._inherited_evidence, store
        )
        metadata["structured_job_candidates"] = _project_candidates(
            self._inherited_candidates, store
        )


def seed_artifact(store: EvidenceStore, artifact: Any) -> None:
    """Promote a chain seed with provenance needed by live-first projection."""
    content = artifact.content or {}
    artifact_aliases = _joined_lists(
        [artifact.artifact_id], content.get("artifact_aliases")
    )
    source_aliases = _joined_lists(
        [artifact.artifact_id], content.get("source_artifact_aliases")
    )
    if artifact.artifact_type == "structured_job_details":
        candidates = content.get("candidates") or content.get("details") or []
        output: dict[str, Any] = {
            "details": [
                {
                    "artifact_id": artifact.artifact_id,
                    "artifact_aliases": artifact_aliases,
                    "source_artifact_id": content.get("source_artifact_id")
                    or artifact.artifact_id,
                    "source_artifact_aliases": source_aliases,
                    "source_url": artifact.source_url or "",
                    "content_hash": artifact.content_hash or "",
                    "source_quality": artifact.quality or "jd_complete",
                    "candidates": candidates,
                    "_runtime_seed": True,
                }
            ]
        }
    else:
        output = {
            "artifact_id": artifact.artifact_id,
            "artifact_aliases": artifact_aliases,
            "source_url": artifact.source_url or "",
            "content_hash": artifact.content_hash or "",
            "_runtime_seed": True,
        }
        if artifact.quality:
            output["quality"] = artifact.quality
        output.update(content)
        output.update(_runtime_seed=True, artifact_aliases=artifact_aliases)
    store.add_observation(
        ToolObservation(
            tool_name=_tool_for_artifact_type(artifact.artifact_type),
            status="succeeded",
            output=output,
        )
    )


def _dict_items(value: Any) -> list[dict[str, Any]]:
    """Copy only JSON-safe inherited records so bad input cannot break refresh."""
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            copied = deepcopy(item)
            json.dumps(copied, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError, OverflowError, RecursionError):
            continue
        items.append(copied)
    return items


def _tool_for_artifact_type(artifact_type: str) -> str:
    return {
        "public_job_page": "fetch-public-job-pages",
        "structured_job_details": "extract-observed-job-details-batch",
        "job_matching_report": "match-observed-jobs",
        "resume_tailoring_brief": "build-resume-tailoring-brief",
    }.get(artifact_type, "fetch-public-job-pages")


def _project_evidence(
    inherited: list[dict[str, Any]], store: EvidenceStore
) -> list[dict[str, Any]]:
    records: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for artifact in _live_then_seeded_artifacts(store, "public_job_page"):
        content = artifact.content or {}
        _add_evidence_record(
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
        _add_evidence_record(records, order, item)
    return _bounded_records(
        [records[key] for key in order], _MAX_EVIDENCE_BYTES, "visible_text"
    )


def _add_evidence_record(
    records: dict[tuple[str, str], dict[str, Any]],
    order: list[tuple[str, str]],
    raw: dict[str, Any],
) -> None:
    source_url = _bounded_string(raw.get("source_url"), _MAX_URL_CHARS)
    content_hash = _bounded_string(raw.get("content_hash"), _MAX_HASH_CHARS)
    visible_text = raw.get("visible_text")
    if not source_url or not content_hash or not _nonempty_string(visible_text):
        return
    key = (source_url, content_hash)
    aliases = _artifact_aliases(raw, content_hash)
    if key in records:
        _merge_aliases(records[key]["artifact_aliases"], aliases, content_hash)
        return
    artifact_id = _first_alias(aliases, f"observed:{content_hash}")
    records[key] = {
        "artifact_id": artifact_id,
        "artifact_aliases": aliases,
        "store_artifact_id": _bounded_string(
            raw.get("store_artifact_id"), _MAX_ID_CHARS
        )
        or "",
        "artifact_type": _bounded_string(raw.get("artifact_type"), 64)
        or "public_job_page",
        "source_url": source_url,
        "content_hash": content_hash,
        "quality": _bounded_string(raw.get("quality"), 64) or "",
        "visible_text": visible_text,
    }
    order.append(key)


def _project_candidates(
    inherited: list[dict[str, Any]], store: EvidenceStore
) -> list[dict[str, Any]]:
    records: dict[tuple[str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str]] = []
    for artifact in _live_then_seeded_artifacts(store, "structured_job_details"):
        content = artifact.content or {}
        candidates = content.get("candidates") or content.get("details") or []
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            _add_candidate_record(
                records,
                order,
                {
                    **candidate,
                    "artifact_id": candidate.get("artifact_id")
                    or content.get("artifact_id")
                    or artifact.artifact_id,
                    "store_artifact_id": artifact.artifact_id,
                    "artifact_aliases": _joined_lists(
                        candidate.get("artifact_aliases"), content.get("artifact_aliases")
                    ),
                    "source_artifact_id": candidate.get("source_artifact_id")
                    or content.get("source_artifact_id")
                    or artifact.artifact_id,
                    "source_artifact_aliases": _joined_lists(
                        candidate.get("source_artifact_aliases"),
                        content.get("source_artifact_aliases"),
                    ),
                    "source_url": candidate.get("source_url") or artifact.source_url,
                    "content_hash": candidate.get("content_hash") or artifact.content_hash,
                    "candidate_id": candidate.get("candidate_id") or candidate.get("id"),
                },
            )
    for item in inherited:
        _add_candidate_record(records, order, item)
    return _bounded_records(
        [records[key] for key in order], _MAX_CANDIDATE_BYTES
    )[:_MAX_CANDIDATES]


def _add_candidate_record(
    records: dict[tuple[str, str, str], dict[str, Any]],
    order: list[tuple[str, str, str]],
    raw: dict[str, Any],
) -> None:
    source_url = _bounded_string(raw.get("source_url"), _MAX_URL_CHARS)
    content_hash = _bounded_string(raw.get("content_hash"), _MAX_HASH_CHARS)
    if not source_url or not content_hash:
        return
    supplied_candidate_id = _bounded_string(
        raw.get("candidate_id") or raw.get("id"), _MAX_ID_CHARS
    )
    role_key = (
        supplied_candidate_id
        if supplied_candidate_id and not _is_store_generated_candidate_id(supplied_candidate_id)
        else _role_key(raw)
    )
    key = (source_url, content_hash, role_key)
    aliases = _candidate_artifact_aliases(
        raw, content_hash, supplied_candidate_id
    )
    source_aliases = _source_artifact_aliases(raw)
    if key in records:
        _merge_aliases(records[key]["artifact_aliases"], aliases, content_hash)
        _merge_aliases(records[key]["source_artifact_aliases"], source_aliases)
        return
    artifact_id = _first_alias(
        _artifact_aliases(raw, content_hash), f"observed:{content_hash}"
    )
    records[key] = {
        "artifact_id": artifact_id,
        "store_artifact_id": _bounded_string(raw.get("store_artifact_id"), _MAX_ID_CHARS) or "",
        "artifact_aliases": aliases,
        "source_artifact_id": _first_alias(source_aliases, artifact_id),
        "source_artifact_aliases": source_aliases,
        "source_url": source_url,
        "content_hash": content_hash,
        "candidate_id": role_key,
        "title": _bounded_string(raw.get("title"), _MAX_ID_CHARS),
        "company": _bounded_string(
            raw.get("company_name") or raw.get("company"), _MAX_ID_CHARS
        ),
        "company_name": _bounded_string(
            raw.get("company_name") or raw.get("company"), _MAX_ID_CHARS
        ),
        "locations": _bounded_strings(raw.get("locations"), 20, 128)
        or _bounded_strings([raw.get("location")], 1, 128),
        "responsibilities": _cap_utf8_bytes(
            _truncated_string(raw.get("responsibilities"), _MAX_TEXT_CHARS) or "",
            _MAX_TEXT_CHARS,
        ),
        "requirements": _cap_utf8_bytes(
            _truncated_string(raw.get("requirements"), _MAX_TEXT_CHARS) or "",
            _MAX_TEXT_CHARS,
        ),
        "source_quality": _bounded_string(raw.get("source_quality"), 64)
        or "jd_complete",
        "page_text_prefix": _cap_utf8_bytes(
            _truncated_string(raw.get("page_text_prefix"), _MAX_PREFIX_CHARS) or "",
            _MAX_PREFIX_CHARS,
        ),
    }
    order.append(key)


def _live_then_seeded_artifacts(store: EvidenceStore, artifact_type: str) -> list[Any]:
    artifacts = [
        artifact for artifact in store.artifacts() if artifact.artifact_type == artifact_type
    ]
    live = [
        artifact for artifact in artifacts if not (artifact.content or {}).get("_runtime_seed")
    ]
    seeded = [
        artifact for artifact in artifacts if (artifact.content or {}).get("_runtime_seed")
    ]
    return [*live, *seeded]


def _bounded_records(
    records: list[dict[str, Any]], budget: int, text_field: str | None = None
) -> list[dict[str, Any]]:
    """Keep complete JSON records within a list's byte budget."""
    projected: list[dict[str, Any]] = []
    used = 2  # ``[]``
    for record in records:
        separator = 1 if projected else 0
        remaining = budget - used - separator
        if remaining <= 0:
            break
        candidate = _fit_text_record(record, text_field, remaining)
        size = _json_size(candidate) if candidate is not None else None
        if size is None or size > remaining:
            continue
        projected.append(candidate)
        used += separator + size
    return projected


def _fit_text_record(
    record: dict[str, Any], text_field: str | None, budget: int
) -> dict[str, Any] | None:
    if text_field is None:
        return record
    value = record.get(text_field)
    if not isinstance(value, str):
        return None
    if (size := _json_size(record)) is not None and size <= budget:
        return record
    low, high, best = 0, len(value), None
    while low <= high:
        mid = (low + high) // 2
        candidate = {**record, text_field: value[:mid]}
        size = _json_size(candidate)
        if size is not None and size <= budget:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def _json_size(value: Any) -> int | None:
    try:
        return len(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None


def _role_key(raw: dict[str, Any]) -> str:
    payload = [
        _bounded_string(raw.get("title"), _MAX_ID_CHARS) or "",
        _bounded_string(raw.get("company_name") or raw.get("company"), _MAX_ID_CHARS)
        or "",
        _bounded_strings(raw.get("locations"), 20, 128),
        _truncated_string(raw.get("responsibilities"), 512) or "",
        _truncated_string(raw.get("requirements"), 512) or "",
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"role:{hashlib.sha256(encoded).hexdigest()}:candidate:0"


def _is_store_generated_candidate_id(value: str) -> bool:
    """True for the legacy ``<uuid4-hex>:candidate:<index>`` fallback."""
    prefix, marker, index = value.partition(":candidate:")
    return (
        bool(marker)
        and index.isdecimal()
        and len(prefix) == 32
        and all(char in "0123456789abcdef" for char in prefix.lower())
    )


def _artifact_aliases(raw: dict[str, Any], content_hash: str) -> list[str]:
    canonical = f"observed:{content_hash}"
    return _limited_aliases(
        [
            _bounded_string(raw.get("artifact_id"), _MAX_ID_CHARS),
            *_bounded_strings(raw.get("artifact_aliases"), _MAX_ALIASES, _MAX_ID_CHARS),
            canonical,
        ],
        canonical=canonical,
    )


def _candidate_artifact_aliases(
    raw: dict[str, Any], content_hash: str, candidate_id: str | None
) -> list[str]:
    """Keep an historical candidate selector even when its store UUID changes."""
    canonical = f"observed:{content_hash}"
    return _limited_aliases(
        [
            _bounded_string(raw.get("artifact_id"), _MAX_ID_CHARS),
            (
                candidate_id
                if candidate_id and _is_store_generated_candidate_id(candidate_id)
                else None
            ),
            *_bounded_strings(raw.get("artifact_aliases"), _MAX_ALIASES, _MAX_ID_CHARS),
            canonical,
        ],
        canonical=canonical,
    )


def _source_artifact_aliases(raw: dict[str, Any]) -> list[str]:
    return _limited_aliases(
        [
            _bounded_string(raw.get("source_artifact_id"), _MAX_ID_CHARS),
            *_bounded_strings(
                raw.get("source_artifact_aliases"), _MAX_ALIASES, _MAX_ID_CHARS
            ),
        ]
    )


def _merge_aliases(
    target: list[str], additions: list[str], canonical: str | None = None
) -> None:
    target[:] = _limited_aliases([*target, *additions], canonical=canonical)


def _limited_aliases(values: list[str | None], canonical: str | None = None) -> list[str]:
    unique = list(
        dict.fromkeys(value for value in values if isinstance(value, str) and value)
    )
    if canonical and canonical in unique:
        unique = [value for value in unique if value != canonical]
        return [*unique[: _MAX_ALIASES - 1], canonical]
    return unique[:_MAX_ALIASES]


def _bounded_strings(value: Any, count: int, chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            item
            for item in value[:count]
            if _bounded_string(item, chars) is not None
        )
    )


def _joined_lists(*values: Any) -> list[Any]:
    return [item for value in values if isinstance(value, list) for item in value]


def _bounded_string(value: Any, limit: int) -> str | None:
    return value if isinstance(value, str) and value and len(value) <= limit else None


def _truncated_string(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _first_alias(values: list[str], fallback: str) -> str:
    return values[0] if values else fallback


def _cap_utf8_bytes(value: str, budget: int) -> str:
    if len(value.encode("utf-8")) <= budget:
        return value
    low, high = 0, len(value)
    while low < high:
        mid = (low + high + 1) // 2
        if len(value[:mid].encode("utf-8")) <= budget:
            low = mid
        else:
            high = mid - 1
    return value[:low]


__all__ = ["RuntimeContextProjection", "safe_private_context", "seed_artifact"]
