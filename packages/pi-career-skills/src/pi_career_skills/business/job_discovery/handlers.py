"""Pure (no network, no DB) job-discovery tool handlers.

These handlers implement the deterministic, evidence-bound logic for the
job-discovery skill tools. Network tools live in :mod:`pi_career_skills.network`
and are replaced in Phase 6.

All handlers are synchronous ``(ToolContext, InputModel) -> OutputModel``
functions. The adapter wraps them with ``asyncio.to_thread`` for the
async AgentTool interface.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit

from ...context import ToolContext
from ...errors import TARGET_EVIDENCE_NOT_FOUND, CareerToolError
from . import jd_extraction
from .models import (
    CandidateIssue,
    DeduplicatedRemoval,
    DeduplicateObservedJobsInput,
    DeduplicateObservedJobsOutput,
    ExtractedJobDetails,
    ExtractObservedJobDetailsBatchInput,
    ExtractObservedJobDetailsBatchOutput,
    ExtractObservedJobDetailsInput,
    ExtractObservedJobDetailsOutput,
    ValidateObservedCandidatesInput,
    ValidateObservedCandidatesOutput,
)
from .target_evidence import resolve_target_evidence
from .title_validation import (
    _extract_portal_role_text,
    _infer_official_page_title,
    _is_plausible_job_title,
)

# ---------------------------------------------------------------------------
# validate-observed-candidates  constants
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# deduplicate-observed-jobs  constants
# ---------------------------------------------------------------------------

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

_ADAPTER_RECORD_KEYS = frozenset({"title", "description", "apply_url"})

# ---------------------------------------------------------------------------
# Shared: evidence lookup
# ---------------------------------------------------------------------------


def _find_observed_evidence(
    context: ToolContext, artifact_id: str
) -> dict[str, Any] | None:
    """Resolve ``artifact_id`` against the run's observed evidence pool.

    The evidence lives in ``context.metadata["observed_public_evidence"]``
    and ``context.metadata["structured_job_candidates"]`` — both are
    already-bounded projections from the harness, never raw model output.
    """
    raw_evidence = context.metadata.get("observed_public_evidence")
    structured_candidates = context.metadata.get("structured_job_candidates", [])
    if isinstance(raw_evidence, list):
        for item in raw_evidence:
            if not isinstance(item, dict):
                continue
            if (
                item.get("artifact_id") == artifact_id
                and item.get("artifact_type") == "job_search_results"
            ):
                raise CareerToolError(
                    TARGET_EVIDENCE_NOT_FOUND,
                    "search_artifact_requires_fetch",
                )
    resolved = resolve_target_evidence(raw_evidence, structured_candidates, artifact_id)
    if resolved is None:
        return None
    # Extract requires page-backed text. A candidate-only projection without
    # a raw page is not enough to manufacture public evidence.
    source_artifact_id = resolved.get("source_artifact_id")
    if source_artifact_id and isinstance(raw_evidence, list):
        for item in raw_evidence:
            if isinstance(item, dict) and (
                item.get("artifact_id") == source_artifact_id
                or f"observed:{item.get('content_hash')}" == source_artifact_id
            ):
                merged = dict(item)
                merged.update(resolved)
                merged["artifact_id"] = item.get("artifact_id") or resolved.get("artifact_id")
                return merged
    return resolved


# ---------------------------------------------------------------------------
# Extract helpers
# ---------------------------------------------------------------------------


def _parse_known_official_career_records(
    text: str, source_url: str
) -> list[dict[str, Any]] | None:
    """Split stable official multi-role career pages into per-JD records."""
    host = (urlsplit(source_url).hostname or "").lower().rstrip(".")
    if host not in {
        "baiontcapital.com",
        "www.baiontcapital.com",
        "baiont.ai",
        "www.baiont.ai",
    }:
        return None
    pattern = re.compile(
        r"^(?P<title>[^\r\n]{2,80})\r?\n"
        r"岗位职责\s*[:：]\s*\r?\n"
        r"(?P<responsibilities>.*?)\r?\n"
        r"要求\s*[:：]\s*\r?\n"
        r"(?P<requirements>.*?)"
        r"(?=\r?\n[^\r\n]{2,80}\r?\n岗位职责\s*[:：]"
        r"|\r?\n您可将简历投递至\s*[:：]?|\Z)",
        flags=re.DOTALL | re.MULTILINE,
    )
    records: list[dict[str, Any]] = []
    for match in pattern.finditer(text):
        title = " ".join(match.group("title").split()).strip()
        responsibilities = " ".join(
            match.group("responsibilities").split()
        ).strip()
        requirements = " ".join(match.group("requirements").split()).strip()
        if not title or not responsibilities:
            continue
        records.append(
            {
                "title": title,
                "company": "倍漾量化",
                "description": (
                    f"岗位职责：{responsibilities}\n任职要求：{requirements}"
                ),
                "apply_url": source_url,
            }
        )
    return records or None


def _parse_adapter_evidence(text: str) -> list[dict[str, Any]] | None:
    """Parse adapter-record JSON evidence; None when the text is not records."""
    if not text.lstrip().startswith("["):
        return None
    try:
        records = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(records, list) or not records:
        return None
    if not all(
        isinstance(record, dict) and _ADAPTER_RECORD_KEYS.issubset(record)
        for record in records
    ):
        return None
    return records


def _record_to_job_details(
    record: dict[str, Any], source_url: str, evidence_ref: dict[str, str]
) -> ExtractedJobDetails:
    """Convert one normalized adapter record to an ExtractedJobDetails."""
    title = record.get("title")
    company_name = record.get("company") or record.get("company_name")
    description = record.get("description", "")
    responsibilities = ""
    requirements = ""
    if description:
        responsibilities, requirements = _split_description(description)
    apply_url = record.get("apply_url") or source_url
    locations_raw = record.get("locations") or record.get("location")
    locations: list[str] = []
    if isinstance(locations_raw, list):
        locations = [str(x) for x in locations_raw if x]
    elif isinstance(locations_raw, str) and locations_raw:
        locations = [locations_raw]
    recruitment_types_raw = record.get("recruitment_types") or record.get("recruitment_type")
    recruitment_types: list[str] = []
    if isinstance(recruitment_types_raw, list):
        recruitment_types = [str(x) for x in recruitment_types_raw if x]
    elif isinstance(recruitment_types_raw, str) and recruitment_types_raw:
        recruitment_types = [recruitment_types_raw]
    confidence = float(record.get("confidence", 0.8))
    return ExtractedJobDetails(
        title=title if isinstance(title, str) else None,
        company_name=company_name if isinstance(company_name, str) else None,
        locations=locations,
        responsibilities=responsibilities,
        requirements=requirements,
        recruitment_types=recruitment_types,
        apply_url=apply_url if isinstance(apply_url, str) else None,
        deadline_text=record.get("deadline") if isinstance(record.get("deadline"), str) else None,
        published_at=record.get("published_at") if isinstance(record.get("published_at"), str) else None,
        confidence=round(confidence, 4),
        evidence_refs=[evidence_ref],
        normalization_warnings=[],
        skills=[str(s) for s in record.get("skills", []) if isinstance(s, str)],
        min_degree=record.get("min_degree") if isinstance(record.get("min_degree"), str) else None,
        priority=record.get("priority", "unknown") if isinstance(record.get("priority"), str) else "unknown",
        strength=record.get("strength") if isinstance(record.get("strength"), dict) else None,
        taxonomy=[str(t) for t in record.get("taxonomy", []) if isinstance(t, str)],
    )


def _split_description(description: str) -> tuple[str, str]:
    """Split a free-text description into responsibilities and requirements."""
    for marker in ("任职要求", "岗位要求", "职位要求", "职责要求", "资格要求", "要求："):
        idx = description.find(marker)
        if idx > 0:
            return description[:idx].strip(), description[idx:].strip()
    return description.strip(), ""


def _adapter_details_output(
    artifact_id: str,
    source_url: str,
    content_hash: str,
    records: list[dict[str, Any]],
    evidence_ref: dict[str, str],
    *,
    source_quality: str | None = None,
) -> ExtractObservedJobDetailsOutput:
    """Build an ExtractObservedJobDetailsOutput from adapter/official records."""
    candidates = [
        _record_to_job_details(record, source_url, evidence_ref)
        for record in records
    ]
    return ExtractObservedJobDetailsOutput(
        source_artifact_id=artifact_id,
        source_url=source_url,
        content_hash=content_hash,
        source_quality=source_quality,  # type: ignore[arg-type]
        candidates=candidates,
    )


def _extract_jd_section(text: str, *, labels: tuple[str, ...]) -> str:
    """Extract one Chinese JD section even when a page collapses line breaks."""
    label_pattern = "|".join(re.escape(label) for label in labels)
    stop_pattern = "|".join(
        re.escape(label)
        for label in (
            "岗位职责",
            "工作职责",
            "职位描述",
            "工作内容",
            "主要职责",
            "任职要求",
            "职责要求",
            "岗位要求",
            "职位要求",
            "资格要求",
            "招聘要求",
            "工作地点",
            "工作地址",
            "投递方式",
            "申请方式",
            "截止日期",
            "截止时间",
            "申请职位",
        )
    )
    pattern = re.compile(
        f"(?:{label_pattern})\\s*[:：]?\\s*\\r?\\n?"
        f"(?P<body>.*?)"
        f"(?=\\r?\\n(?:{stop_pattern})|$)",
        flags=re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(text)
    if not match:
        return ""
    body = match.group("body").strip()
    # Strip a couple of common decorative dividers.
    body = re.sub(r"[-_—―]{3,}", "", body).strip()
    return body


def _prepare_portal_extraction_text(text: str, source_url: str) -> str:
    """Strip obvious portal chrome before JD extraction (pure text surgery).

    Only removes navigation/header/footer that is deterministically
    identifiable; never adds or rewrites JD content.
    """
    if not text:
        return text
    lines = text.splitlines()
    # Drop top navigation lines (common portal headers).
    header_markers = ("首页", "招聘首页", "校园招聘", "社会招聘", "关于我们", "加入我们")
    start = 0
    for i, line in enumerate(lines[:30]):
        stripped = line.strip()
        if not stripped:
            continue
        if any(marker in stripped for marker in header_markers):
            start = i + 1
        else:
            break
    # Drop footer lines.
    footer_markers = ("©", "版权所有", "备案号", "京ICP", "沪ICP", "粤ICP", "隐私政策", "服务条款")
    end = len(lines)
    for i in range(len(lines) - 1, max(start, len(lines) - 40), -1):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if any(marker in stripped for marker in footer_markers):
            end = i
            break
    return "\n".join(lines[start:end]).strip()


def _is_richer_official_title(inferred: str | None, extracted: str | None) -> bool:
    """True when the inferred page title is more specific than the extracted one."""
    if not inferred or not extracted:
        return False
    return extracted.lower() in inferred.lower()


def _infer_official_page_locations(text: str, title: str | None) -> list[str]:
    """Infer location from a JD page's header/metadata area."""
    if not text:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # Scan the first ~40 lines for location-like patterns.
    location_patterns = [
        re.compile(r"工作地点\s*[:：]\s*(.+)"),
        re.compile(r"工作地址\s*[:：]\s*(.+)"),
        re.compile(r"地点\s*[:：]\s*(.+)"),
        re.compile(r"base\s*[:：]?\s*(.+)", re.IGNORECASE),
    ]
    found: list[str] = []
    for line in lines[:40]:
        for pat in location_patterns:
            m = pat.search(line)
            if m:
                loc = m.group(1).strip().rstrip("。，,;；")
                if loc and len(loc) < 60:
                    found.append(loc)
                    break
        if found:
            break
    if not found and title:
        # Try bracketed location in title, e.g. "算法工程师（北京）"
        m = re.search(r"[（(]([^）)]{2,20})[）)]", title)
        if m:
            loc = m.group(1).strip()
            if len(loc) < 20 and not _is_plausible_job_title(loc):
                found.append(loc)
    return found


def _infer_recruitment_types(source_url: str, fallback: list[str]) -> list[str]:
    """Infer recruitment_type from URL shape when extraction didn't capture it."""
    if fallback:
        return fallback
    lowered = source_url.lower()
    if "campus" in lowered or "xiaoyuan" in lowered or "xiaozhao" in lowered:
        return ["campus"]
    if "intern" in lowered or "shixi" in lowered:
        return ["internship"]
    if "social" in lowered or "shezhao" in lowered:
        return ["social"]
    return []


# ---------------------------------------------------------------------------
# extract-observed-job-details
# ---------------------------------------------------------------------------


def extract_observed_job_details(
    context: ToolContext, payload: ExtractObservedJobDetailsInput
) -> ExtractObservedJobDetailsOutput:
    """Normalize one existing public-page artifact without accepting model text."""
    evidence = _find_observed_evidence(context, payload.artifact_id)
    if evidence is None:
        raise CareerToolError(TARGET_EVIDENCE_NOT_FOUND, payload.artifact_id)
    source_url = evidence.get("source_url")
    content_hash = evidence.get("content_hash")
    visible_text = evidence.get("visible_text")
    if not all(isinstance(value, str) and value for value in (source_url, content_hash, visible_text)):
        raise CareerToolError(TARGET_EVIDENCE_NOT_FOUND, "evidence missing required fields")
    evidence_ref = {
        "artifact_id": payload.artifact_id,
        "source_url": source_url,
        "content_hash": content_hash,
    }
    source_quality = evidence.get("quality")
    if source_quality not in {"jd_complete", "list_only", "js_shell", "empty"}:
        source_quality = None
    official_records = _parse_known_official_career_records(visible_text, source_url)
    if official_records is not None:
        return _adapter_details_output(
            payload.artifact_id,
            source_url,
            content_hash,
            official_records,
            evidence_ref,
            source_quality=source_quality,
        )
    adapter_records = _parse_adapter_evidence(visible_text)
    if adapter_records is not None:
        return _adapter_details_output(
            payload.artifact_id,
            source_url,
            content_hash,
            adapter_records,
            evidence_ref,
            source_quality=source_quality,
        )
    # Several public campus portals put the real title/company in a detail
    # header but place navigation chrome before the JD body.
    extraction_text = _prepare_portal_extraction_text(visible_text, source_url)
    extracted = jd_extraction.extract_jd_candidates(extraction_text, source_url)
    if source_quality == "jd_complete" and "/job/" in urlsplit(source_url).path:
        official_title = _infer_official_page_title(visible_text)
        if official_title:
            official_matches = [
                candidate
                for candidate in extracted
                if isinstance(candidate.title, str)
                and candidate.title.strip().lower() in official_title.lower()
            ]
            if official_matches:
                extracted = official_matches[:1]
            elif extracted:
                extracted = extracted[:1]
    # A single-JD page is enriched from its own full text; a multi-candidate
    # page must NOT have the page's first responsibilities/requirements
    # section copied onto every candidate.
    single_jd_page = len(extracted) <= 1
    candidates: list[ExtractedJobDetails] = []
    for candidate in extracted:
        inferred_title = _infer_official_page_title(visible_text)
        title = (
            inferred_title
            if candidate.title in {None, "申请职位"}
            or (
                inferred_title is not None
                and (
                    not _is_plausible_job_title(candidate.title)
                    or _is_richer_official_title(
                        inferred_title, candidate.title
                    )
                )
            )
            else candidate.title
        )
        locations = candidate.locations or _infer_official_page_locations(
            visible_text, title
        )
        recruitment_types = _infer_recruitment_types(
            source_url, candidate.recruitment_types
        )
        warnings = [
            warning
            for warning in candidate.normalization_warnings
            if not (locations and warning == "No location information found")
        ]
        responsibilities = (
            _extract_jd_section(
                extraction_text,
                labels=("岗位职责", "工作职责", "职位描述", "工作内容", "主要职责", "岗位定位", "你将负责"),
            )
            if single_jd_page
            else candidate.responsibilities
        ) or candidate.responsibilities
        portal_role_text = _extract_portal_role_text(visible_text, source_url)
        if portal_role_text and portal_role_text not in responsibilities:
            responsibilities = " ".join(
                part for part in (responsibilities, portal_role_text) if part
            )
        requirements = (
            _extract_jd_section(
                extraction_text,
                labels=(
                    "任职要求",
                    "职责要求",
                    "岗位要求",
                    "职位要求",
                    "资格要求",
                    "招聘要求",
                    "任职资格",
                    "专业要求",
                ),
            )
            if single_jd_page
            else candidate.requirements
        ) or candidate.requirements
        candidates.append(
            ExtractedJobDetails(
                title=title,
                company_name=candidate.company_name,
                locations=locations,
                responsibilities=responsibilities,
                requirements=requirements,
                recruitment_types=recruitment_types,
                apply_url=candidate.apply_url,
                deadline_text=candidate.deadline_text,
                published_at=candidate.published_at,
                confidence=round(candidate.confidence, 4),
                evidence_refs=[evidence_ref],
                normalization_warnings=warnings,
                skills=candidate.skills,
                min_degree=candidate.min_degree,
                priority=candidate.priority,
                strength=candidate.strength,
                taxonomy=candidate.taxonomy,
            )
        )
    return ExtractObservedJobDetailsOutput(
        source_artifact_id=payload.artifact_id,
        source_url=source_url,
        content_hash=content_hash,
        source_quality=source_quality,  # type: ignore[arg-type]
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# extract-observed-job-details-batch
# ---------------------------------------------------------------------------


def extract_observed_job_details_batch(
    context: ToolContext, payload: ExtractObservedJobDetailsBatchInput
) -> ExtractObservedJobDetailsBatchOutput:
    """Normalize a bounded observed set without letting the model supply JD text."""
    return ExtractObservedJobDetailsBatchOutput(
        details=[
            extract_observed_job_details(
                context, ExtractObservedJobDetailsInput(artifact_id=artifact_id)
            )
            for artifact_id in payload.artifact_ids
        ]
    )


# ---------------------------------------------------------------------------
# validate-observed-candidates
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# deduplicate-observed-jobs
# ---------------------------------------------------------------------------


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
    "extract_observed_job_details",
    "extract_observed_job_details_batch",
    "validate_observed_candidates",
    "deduplicate_observed_jobs",
]
