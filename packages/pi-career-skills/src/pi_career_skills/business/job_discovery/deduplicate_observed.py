"""deduplicate-observed-jobs handler (Phase 6 §5 move + P2 semantic upgrade).

Verbatim move of the dedup block (``deduplicate_observed_jobs`` plus the
identity-key helpers and constants) from ``business/job_discovery/handlers.py``.
The pi adaptation noted in §5 is kept: ``_evidence_identity_keys`` catches
``CareerToolError`` (the source catches ``PublicJobFetchError``; on the pi
side the extract handler raises the pi error type, which is the superset).

P2 layers three upgrades on top of the P0 first-seen-wins identity, with no
change to the tool contract:

* **content-hash identity** — two observed artifacts whose normalized page
  text hashes match are the same page (``hash:`` key, strongest).
* **company-scoped titles** — the exact-title key carries the company when it
  is known, so same-named roles at *different* companies are no longer merged
  (the old key merged them blindly).
* **echo drop** — a short, non-JD snippet (search result / list card) that
  shares a title skeleton with a much richer page of the same company is
  dropped as ``echo_of`` the richer artifact, *whichever came first in run
  order* (the richer artifact wins, not the earlier one).

The weak ``title-skel:`` cluster key is only ever used to find a possible
echo partner — it never removes an artifact on its own.

Cycle note: this module used to import ``_find_observed_evidence``,
``_parse_adapter_evidence`` and ``extract_observed_job_details`` from
``.handlers`` at module level while ``.handlers`` re-exports
``deduplicate_observed_jobs`` at the bottom of the module (``# noqa: E402``) —
a genuine import cycle that only resolved when ``handlers`` happened to load
first.  Those three names are now imported lazily inside the functions that
use them, so this module loads standalone and the re-export cycle is gone.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

from ...context import ToolContext
from ...errors import CareerToolError
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

# --- P2 echo detection ------------------------------------------------------
# An "echo" is a short, non-JD snippet (search result, list card) of a job
# whose full JD was captured as a separate, much richer artifact.
_ECHO_KEEPER_MIN_CHARS = 500  # the surviving artifact must be a real page
_ECHO_CEILING = 1500          # an echo must be snippet-scale (< this many chars)
_ECHO_MIN_RATIO = 4           # the keeper must be >= 4x the echo's text
_BRACKETED_RE = re.compile(r"（[^（）]*）|\([^()]*\)|【[^【】]*】|\[[^\[\]]*\]")
_ECHO_NOISE_TOKENS = (
    "急聘",
    "诚聘",
    "急招",
    "诚招",
    "高薪",
    "热招",
    "火热招聘",
    "长期有效",
    "五险一金",
    "提供住宿",
)


@dataclass(frozen=True)
class _ArtifactIdentity:
    """Identity + echo metadata for one observed evidence artifact."""

    keys: tuple[str, ...]  # hard identity keys, in priority order
    cluster_key: str | None  # weak title-skeleton key (echo detection only)
    company: str | None  # normalized company, when known
    text_len: int
    quality: str | None


def deduplicate_observed_jobs(
    context: ToolContext, payload: DeduplicateObservedJobsInput
) -> DeduplicateObservedJobsOutput:
    """Dedupe observed artifacts by canonical identity, preserving run order.

    P2 semantics layered on the P0 first-seen-wins identity: content-hash
    identity (same normalized page), company-scoped exact titles (same-named
    roles at different companies are not merged), and echo drop (a short
    snippet is dropped in favor of the same job's much richer page, whichever
    artifact came first).
    """
    # Lazy import: .handlers re-exports us at its bottom, so a module-level
    # import here would cycle.  By call time both modules are fully loaded.
    from .handlers import _find_observed_evidence

    kept: list[str] = []
    removed: list[DeduplicatedRemoval] = []
    seen_hard: dict[str, str] = {}
    seen_cluster: dict[str, str] = {}
    identities: dict[str, _ArtifactIdentity] = {}
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
        identity = _evidence_identity(context, artifact_id, evidence)
        if not identity.keys and identity.cluster_key is None:
            kept.append(artifact_id)
            continue
        owner_hard = _first_key_owner(identity.keys, seen_hard)
        owner_cluster = (
            seen_cluster.get(identity.cluster_key)
            if identity.cluster_key is not None
            else None
        )
        owner = owner_hard or owner_cluster
        weak_only = owner_hard is None and owner_cluster is not None
        if owner is None:
            kept.append(artifact_id)
            identities[artifact_id] = identity
            _register_identity(identity, artifact_id, seen_hard, seen_cluster)
            continue
        owner_identity = identities.get(owner)
        cand_echo = owner_identity is not None and _is_echo_of(identity, owner_identity)
        owner_echo = owner_identity is not None and _is_echo_of(owner_identity, identity)
        if weak_only and not cand_echo and not owner_echo:
            # A skeleton-only match between two real pages is not a duplicate —
            # distinct jobs happen to share a title skeleton.  Keep both; the
            # first still owns the cluster slot for future echo detection.
            kept.append(artifact_id)
            identities[artifact_id] = identity
            _register_identity(identity, artifact_id, seen_hard, seen_cluster)
            continue
        if cand_echo:
            removed.append(
                DeduplicatedRemoval(
                    artifact_id=artifact_id,
                    reason="echo_of",
                    detail=f"snippet echo of kept artifact {owner}",
                )
            )
            continue
        if owner_echo:
            # The kept artifact is the snippet and the new one is the full JD:
            # replace it in place so the richer artifact survives.
            removed.append(
                DeduplicatedRemoval(
                    artifact_id=owner,
                    reason="echo_of",
                    detail=f"snippet echo of kept artifact {artifact_id}",
                )
            )
            kept = [item for item in kept if item != owner]
            identities.pop(owner, None)
            for key, value in seen_hard.items():
                if value == owner:
                    seen_hard[key] = artifact_id
            for key, value in seen_cluster.items():
                if value == owner:
                    seen_cluster[key] = artifact_id
            kept.append(artifact_id)
            identities[artifact_id] = identity
            continue
        removed.append(
            DeduplicatedRemoval(
                artifact_id=artifact_id,
                reason="duplicate_identity",
                detail=f"shares identity with kept artifact {owner}",
            )
        )
    return DeduplicateObservedJobsOutput(kept=kept, removed=removed)


def _first_key_owner(
    keys: tuple[str, ...], seen_hard: dict[str, str]
) -> str | None:
    for key in keys:
        if key in seen_hard:
            return seen_hard[key]
    return None


def _register_identity(
    identity: _ArtifactIdentity,
    artifact_id: str,
    seen_hard: dict[str, str],
    seen_cluster: dict[str, str],
) -> None:
    for key in identity.keys:
        seen_hard.setdefault(key, artifact_id)
    if identity.cluster_key is not None:
        seen_cluster.setdefault(identity.cluster_key, artifact_id)


def _is_echo_of(candidate: _ArtifactIdentity, keeper: _ArtifactIdentity) -> bool:
    """True when *candidate* looks like a snippet echo of the richer *keeper*.

    The keeper must be a substantial page, the candidate must be snippet-scale
    and not itself a complete JD, the keeper must be at least
    ``_ECHO_MIN_RATIO``x the candidate's text, and the companies must be
    compatible (identical when both are known).
    """
    return (
        _companies_compatible(candidate.company, keeper.company)
        and keeper.text_len >= _ECHO_KEEPER_MIN_CHARS
        and candidate.quality != "jd_complete"
        and candidate.text_len < _ECHO_CEILING
        and candidate.text_len * _ECHO_MIN_RATIO < keeper.text_len
    )


def _companies_compatible(left: str | None, right: str | None) -> bool:
    """Unknown companies never disprove a match; known ones must agree."""
    return left is None or right is None or left == right


def _evidence_identity(
    context: ToolContext, artifact_id: str, evidence: dict[str, object]
) -> _ArtifactIdentity:
    """Identity + echo metadata for one artifact (parse/extract runs once)."""
    # Lazy import: .handlers re-exports us at its bottom (module-level import
    # would cycle).  By call time both modules are fully loaded.
    from .handlers import _parse_adapter_evidence, extract_observed_job_details

    text = str(evidence["visible_text"])
    quality = evidence.get("quality")
    if not isinstance(quality, str):
        quality = None
    keys: list[str] = []
    content_hash = evidence.get("content_hash")
    if isinstance(content_hash, str) and content_hash:
        keys.append(f"hash:{content_hash}")
    cluster_key: str | None = None
    company: str | None = None
    records = _parse_adapter_evidence(text)
    if records is not None:
        for record in records:
            keys.extend(_record_identity_keys(record))
            if cluster_key is None:
                cluster_key, company = _record_cluster(record)
    else:
        try:
            output = extract_observed_job_details(
                context, ExtractObservedJobDetailsInput(artifact_id=artifact_id)
            )
        except CareerToolError:
            # Unparseable page: the hash key above still dedupes exact copies.
            return _ArtifactIdentity(
                tuple(dict.fromkeys(keys)), None, None, len(text), quality
            )
        for candidate in output.candidates:
            keys.extend(_detail_identity_keys(candidate))
            if cluster_key is None:
                cluster_key = _detail_cluster(candidate)
                company = _normalize_company(candidate.company_name)
    return _ArtifactIdentity(
        tuple(dict.fromkeys(keys)), cluster_key, company, len(text), quality
    )


def _evidence_identity_keys(
    context: ToolContext, artifact_id: str, evidence: dict[str, object]
) -> tuple[str, ...]:
    """All canonical identity keys one artifact claims, in priority order."""
    return _evidence_identity(context, artifact_id, evidence).keys


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
    company = record.get("company") or record.get("company_name")
    title_key = _title_identity(
        title if isinstance(title, str) else None,
        locations,
        [],
        company=_normalize_company(company if isinstance(company, str) else None),
    )
    return tuple(key for key in (url_key, title_key) if key)


def _detail_identity_keys(detail: ExtractedJobDetails) -> tuple[str, ...]:
    """Identity keys for one extracted JD candidate (page-text path)."""
    url_key = _url_identity(detail.apply_url)
    if url_key:
        return (url_key,)
    title_key = _title_identity(
        detail.title,
        detail.locations,
        detail.recruitment_types,
        company=_normalize_company(detail.company_name),
    )
    return (title_key,) if title_key else ()


def _record_cluster(
    record: dict[str, object],
) -> tuple[str | None, str | None]:
    """Weak title-skeleton cluster key + company for one adapter record."""
    title = record.get("title")
    skeleton = _title_skeleton(title if isinstance(title, str) else None)
    if not skeleton:
        return None, None
    company = record.get("company") or record.get("company_name")
    return (
        f"title-skel:{skeleton}",
        _normalize_company(company if isinstance(company, str) else None),
    )


def _detail_cluster(detail: ExtractedJobDetails) -> str | None:
    """Weak title-skeleton cluster key for one extracted JD candidate."""
    skeleton = _title_skeleton(detail.title)
    return f"title-skel:{skeleton}" if skeleton else None


def _title_skeleton(title: str | None) -> str:
    """Title reduced to its role skeleton: all bracketed qualifiers and
    common recruitment noise removed, then the normal title normalization."""
    if not title or not title.strip():
        return ""
    text = _BRACKETED_RE.sub("", title)
    for token in _ECHO_NOISE_TOKENS:
        text = text.replace(token, "")
    return _normalize_title(text)


def _normalize_company(name: str | None) -> str | None:
    """Normalize a company name for scoping/comparison; None when unknown."""
    if not name or not name.strip():
        return None
    text = name
    for ch in _INVISIBLE_CHARS:
        text = text.replace(ch, "")
    text = unicodedata.normalize("NFKC", text)
    text = _WHITESPACE_RE.sub("", text)
    text = text.translate(_DELETE_TABLE)
    normalized = text.lower().strip()
    return normalized or None


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
    *,
    company: str | None = None,
) -> str:
    """Normalized-title identity, scoped by location/recruitment type/company.

    The company component is only added when the company is known: an
    unknown-company title key never collides with a known-company one, so we
    prefer keeping both over wrongly merging two employers' same-named roles.
    """
    if not title or not title.strip():
        return ""
    normalized = _normalize_title(title)
    if not normalized:
        return ""
    loc_key = "+".join(sorted(locations)) if locations else ""
    rt_key = "+".join(sorted(recruitment_types)) if recruitment_types else ""
    company_key = f"|co:{company}" if company else ""
    return f"title:{normalized}|loc:{loc_key}|rt:{rt_key}{company_key}"


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
    "_ECHO_KEEPER_MIN_CHARS",
    "_ECHO_CEILING",
    "_ECHO_MIN_RATIO",
    "_ECHO_NOISE_TOKENS",
    "deduplicate_observed_jobs",
    "_evidence_identity",
    "_evidence_identity_keys",
    "_is_echo_of",
    "_record_identity_keys",
    "_detail_identity_keys",
    "_record_cluster",
    "_detail_cluster",
    "_title_skeleton",
    "_normalize_company",
    "_url_identity",
    "_normalize_apply_url",
    "parse_query_safe",
    "_title_identity",
    "_normalize_title",
]
