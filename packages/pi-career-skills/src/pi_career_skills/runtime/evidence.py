"""In-memory evidence store for one run.

The trusted kernel is the sole authority for what counts as evidence:

* Only **succeeded** tool observations are considered.
* Only tools with an entry in ``TOOL_ARTIFACT_TYPE`` produce artifacts.
* Artifact content is bounded before storage (field / string / list caps).
* A content hash anchors every artifact; matching and tailoring artifacts
  use the canonical-JSON sha256 of their payload.
* Shells (empty search, blocked pages, nav-label titles) never enter the
  job-bearing set.
* Artifact IDs are store-generated (uuid4 hex); the model never supplies them.

Thread-safe: ``threading.Lock`` wraps every write so to_thread handlers and
the controller can both append observations.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Mapping
from typing import Any

from ..contracts import Artifact, ToolObservation

# ---------------------------------------------------------------------------
# Local copies of registry / tool-adapter values.
#
# The canonical source is ``pi_career_skills.registry.TOOL_ARTIFACT_TYPE``
# and ``pi_career_skills.tool_adapter.bound_content``.  We re-import when
# available and fall back to these verbatim mirrors so the module is
# self-contained for testing before the parallel Task D lands.
# ---------------------------------------------------------------------------

try:  # pragma: no cover - import-only branch
    from ..registry import TOOL_ARTIFACT_TYPE
except ImportError:  # pragma: no cover - parallel-task gap
    #: tool_name -> artifact_type.  Mirrors
    #: ``backend/app/services/career_skills/registry.py`` verbatim.
    TOOL_ARTIFACT_TYPE: dict[str, str] = {
        "fetch-public-job-pages": "public_job_page",
        "fetch-public-job-page": "public_job_page",
        "fetch-wechat-article": "public_job_page",
        "search-public-job-pages": "job_search_results",
        "query-career-sheet-records": "job_search_results",
        "extract-observed-job-details": "structured_job_details",
        "extract-observed-job-details-batch": "structured_job_details",
        "match-observed-jobs": "job_matching_report",
        "build-resume-tailoring-brief": "resume_tailoring_brief",
    }

try:  # pragma: no cover - import-only branch
    from ..tool_adapter import bound_content
except ImportError:  # pragma: no cover - parallel-task gap

    def bound_content(value: Mapping[str, Any]) -> dict[str, Any]:
        """Return a bounded copy of *value* (40 / 12_000 / 20 / 1_200).

        Mirrors ``backend/app/services/agent_kernel/evidence.py``
        ``_bounded_content`` exactly.
        """
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:40]:
            if isinstance(item, str):
                result[str(key)] = item[:12_000]
            elif isinstance(item, (int, float, bool)) or item is None:
                result[str(key)] = item
            elif isinstance(item, list):
                result[str(key)] = [
                    (
                        bound_content(nested)
                        if isinstance(nested, Mapping)
                        else str(nested)[:1_200]
                    )
                    for nested in item[:20]
                ]
            elif isinstance(item, Mapping):
                result[str(key)] = bound_content(item)
        return result


# ---------------------------------------------------------------------------
# Semantic-validation constants — verbatim from completion.py source.
# ---------------------------------------------------------------------------

#: Navigation / UI labels that are never real job titles.
_NAV_LABEL_TITLES = frozenset(
    {
        "浏览职位",
        "查看全部",
        "招聘观察",
        "申请职位",
        "职位",
        "岗位",
        "职位列表",
        "岗位列表",
        "热门职位",
        "招聘职位",
        "招聘日历",
        "首页",
        "招聘",
        "投递",
        "登录",
        "注册",
        "联系我们",
        "关于我们",
        "返回",
        "更多",
    }
)

#: Minimum combined responsibilities+requirements chars for a usable JD body.
_MIN_JD_BODY_CHARS = 20

#: Artifact types that can anchor a job-bearing deliverable.
_JOB_BEARING_ARTIFACT_TYPES = frozenset(
    {
        "public_job_page",
        "structured_job_details",
        "job_matching_report",
        "resume_tailoring_brief",
        "career_preparation_plan",
    }
)


# ---------------------------------------------------------------------------
# Canonical hash helpers
# ---------------------------------------------------------------------------


def canonical_json(value: Mapping[str, Any]) -> str:
    """Return the canonical JSON representation used for content hashing."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash_of(artifact: Artifact) -> str:
    """Return the content hash recorded on *artifact* (64-char lowercase hex)."""
    return artifact.content_hash


# ---------------------------------------------------------------------------
# Semantic validators
# ---------------------------------------------------------------------------


def _is_plausible_job_title(value: Any) -> bool:
    """True when *value* looks like a real job title (not a nav label).

    Verbatim mirror of ``completion.py:_is_plausible_job_title``.
    """
    if not isinstance(value, str):
        return False
    title = value.strip()
    if len(title) < 2:
        return False
    if title in _NAV_LABEL_TITLES:
        return False
    return any(
        ("一" <= ch <= "鿿") or ch.isascii() and ch.isalpha() for ch in title
    )


def _has_real_structured_candidate(content: Mapping[str, Any]) -> bool:
    """True when *content* has at least one plausible JD candidate.

    Verbatim mirror of ``completion.py:_has_real_structured_candidate``.
    """
    candidates = content.get("candidates") or content.get("details") or []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        if not _is_plausible_job_title(candidate.get("title")):
            continue
        body = (
            f"{candidate.get('responsibilities') or ''} "
            f"{candidate.get('requirements') or ''}"
        ).strip()
        if len(body) >= _MIN_JD_BODY_CHARS:
            return True
    return False


def _validate_public_job_page(content: Mapping[str, Any]) -> bool:
    """``quality == "jd_complete"`` is the gate for public_job_page artifacts."""
    return content.get("quality") == "jd_complete"


def _validate_structured_job_details(content: Mapping[str, Any]) -> bool:
    """At least one candidate has a plausible title + >= 20-char body."""
    return _has_real_structured_candidate(content)


def _validate_job_matching_report(content: Mapping[str, Any]) -> bool:
    """A matching report is valid when either:

    * ``matches`` is non-empty, OR
    * ``matches`` is empty but there is an explicit ``no_match_reason`` /
      ``no_candidate_satisfied_constraints`` flag **and** an
      ``evaluated_candidate_count`` proving the matcher actually ran.
    """
    matches = content.get("matches")
    if not isinstance(matches, list):
        return False
    if matches:
        return True
    # The handler emits ``no_match_reason`` with the single literal
    # ``no_candidate_satisfied_constraints`` (or the flag form) only, so any
    # other reason (e.g. ``budget_exhausted``) must NOT label the report
    # job-bearing — the completion gate requires the exact value too.
    has_reason = bool(
        content.get("no_candidate_satisfied_constraints")
        or content.get("no_match_reason") == "no_candidate_satisfied_constraints"
    )
    has_trace = "evaluated_candidate_count" in content
    return has_reason and has_trace


def _validate_resume_tailoring_brief(content: Mapping[str, Any]) -> bool:
    """A tailoring brief needs target_artifact_id, source_url, and non-empty
    safe_actions."""
    target = content.get("target_artifact_id")
    source_url = content.get("source_url")
    actions = content.get("safe_actions")
    if not isinstance(target, str) or not target.strip():
        return False
    if not isinstance(source_url, str) or not source_url.strip():
        return False
    return isinstance(actions, list) and bool(actions)


_VALIDATORS: dict[str, Any] = {
    "public_job_page": _validate_public_job_page,
    "structured_job_details": _validate_structured_job_details,
    "job_matching_report": _validate_job_matching_report,
    "resume_tailoring_brief": _validate_resume_tailoring_brief,
}


def _is_quality_job_bearing(artifact: Artifact) -> bool:
    """True when *artifact* counts as a real, job-bearing deliverable."""
    if artifact.artifact_type not in _JOB_BEARING_ARTIFACT_TYPES:
        return False
    validator = _VALIDATORS.get(artifact.artifact_type)
    if validator is None:
        # Types in the set without an explicit validator are type-valid.
        return True
    return validator(artifact.content or {})


# ---------------------------------------------------------------------------
# Candidate extraction + shell detection
# ---------------------------------------------------------------------------


def _extract_candidates(output: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Pull per-page / per-detail candidate dicts out of a tool output.

    Mirrors ``evidence.py:_candidates``: the top-level dict counts if it has
    both source_url and content_hash, plus items under ``pages`` / ``details``
    / ``candidates`` / ``results``.
    """
    candidates: list[dict[str, Any]] = []
    if isinstance(output.get("source_url"), str) and isinstance(
        output.get("content_hash"), str
    ):
        candidates.append(dict(output))
    for key in ("pages", "details", "candidates", "results"):
        value = output.get(key)
        if isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, dict))
    return candidates


def _is_empty_search_shell(candidates: list[dict[str, Any]]) -> bool:
    """True when a job_search_results output carries zero usable results.

    Mirrors ``evidence.py:_is_empty_search_shell``: the first candidate is the
    search-page shell itself; it is empty when results is missing/empty AND
    terminal_reason is ``search_empty`` or ``blocked``.
    """
    if not candidates:
        return False
    shell = candidates[0]
    results = shell.get("results")
    if isinstance(results, list) and results:
        return False
    terminal_reason = shell.get("terminal_reason")
    return terminal_reason in {"search_empty", "blocked"}


# ---------------------------------------------------------------------------
# EvidenceStore
# ---------------------------------------------------------------------------


class EvidenceStore:
    """In-memory evidence + artifact store for a single run.

    The only write entry point is :meth:`add_observation`.  Artifact IDs are
    store-generated; callers (and therefore the model) never supply one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._artifacts: list[Artifact] = []
        self._by_id: dict[str, Artifact] = {}
        # dedup key -> artifact_id
        self._dedup: dict[tuple[str, str, str], str] = {}

    # -- write entry point -------------------------------------------------

    def add_observation(
        self,
        obs: ToolObservation,
        tool_name_artifact_type: str | None = None,
    ) -> list[Artifact]:
        """Promote *obs* to zero or more artifacts and return them.

        Only ``status == "succeeded"`` observations are considered.  Shells
        (empty search, blocked pages, nav-label-only structured details) are
        skipped for the job-bearing set but may still be stored as low-quality
        artifacts — currently we return the empty list for them to keep the
        store deliverables-only; routing evidence is recorded elsewhere.

        Parameters
        ----------
        obs:
            The tool observation to promote.
        tool_name_artifact_type:
            Optional explicit artifact type override.  When ``None``, the type
            is looked up via ``TOOL_ARTIFACT_TYPE[obs.tool_name]``.

        Returns
        -------
        list[Artifact]
            Artifacts created (or dedup-hit) for this observation.  Empty when
            the observation failed, had no mapping, or produced only shells.
        """
        if obs.status != "succeeded":
            return []

        artifact_type = tool_name_artifact_type or TOOL_ARTIFACT_TYPE.get(
            obs.tool_name
        )
        if artifact_type is None:
            return []

        output = obs.output or {}
        if not isinstance(output, Mapping):
            return []

        # Shell filter: job_search_results with no usable results is routing
        # evidence, not a deliverable — never promote it.
        if artifact_type == "job_search_results":
            candidates = _extract_candidates(output)
            if _is_empty_search_shell(candidates):
                return []

        # Derive hash-anchor candidates per artifact type.
        raw_candidates = self._materialize_candidates(artifact_type, output)
        if not raw_candidates:
            return []

        promoted: list[Artifact] = []
        with self._lock:
            for raw in raw_candidates:
                artifact = self._promote_one_locked(
                    artifact_type=artifact_type,
                    tool_name=obs.tool_name,
                    raw=raw,
                )
                if artifact is not None:
                    promoted.append(artifact)
        return promoted

    # -- read accessors ----------------------------------------------------

    def artifacts(self) -> list[Artifact]:
        """Return all stored artifacts in insertion order."""
        with self._lock:
            return list(self._artifacts)

    def job_bearing_artifacts(self) -> list[Artifact]:
        """Return artifacts that pass the quality job-bearing gate."""
        with self._lock:
            return [a for a in self._artifacts if _is_quality_job_bearing(a)]

    def get(self, artifact_id: str) -> Artifact | None:
        """Look up an artifact by id, or ``None``."""
        with self._lock:
            return self._by_id.get(artifact_id)

    def by_source_url(self, url: str) -> list[Artifact]:
        """Return all artifacts whose source_url equals *url*."""
        with self._lock:
            return [a for a in self._artifacts if a.source_url == url]

    def refs(self, last: int = 12) -> list[dict[str, str]]:
        """Return the *last* most recent artifact reference projections.

        Each ref contains ``artifact_id``, ``artifact_type``, ``source_url``,
        ``content_hash``, and ``quality`` — no full content, so this is safe
        to project into the next model invocation.
        """
        with self._lock:
            tail = self._artifacts[-last:] if last > 0 else []
        return [
            {
                "artifact_id": a.artifact_id,
                "artifact_type": a.artifact_type,
                "source_url": a.source_url,
                "content_hash": a.content_hash,
                "quality": a.quality or "",
            }
            for a in tail
        ]

    # -- internal helpers --------------------------------------------------

    def _materialize_candidates(
        self, artifact_type: str, output: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        """Build per-candidate dicts from a tool output.

        For ``public_job_page`` and ``structured_job_details`` we use the
        generic ``_extract_candidates`` helper (top-level + nested lists).
        For ``job_matching_report`` we synthesize a single report candidate
        anchored to the first match's source_url (when present).
        For ``resume_tailoring_brief`` we use the top-level dict as the
        candidate.
        """
        if artifact_type == "job_matching_report":
            matches = output.get("matches")
            if not isinstance(matches, list):
                return []
            canonical = canonical_json(dict(output))
            report = dict(output)
            report["content_hash"] = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()
            if matches and isinstance(matches[0], Mapping):
                anchor = matches[0].get("source_url")
                if isinstance(anchor, str) and anchor.strip():
                    report["source_url"] = anchor
            # Empty-match report (no_candidate_satisfied_constraints) has no
            # anchor URL — source_url stays None; the promotion rule below
            # admits it as a derived deliverable (migration plan §6.5).
            return [report]

        if artifact_type == "resume_tailoring_brief":
            if not isinstance(output.get("source_url"), str):
                return []
            canonical = canonical_json(dict(output))
            brief = dict(output)
            brief["content_hash"] = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()
            return [brief]

        if artifact_type == "career_preparation_plan":
            if not isinstance(output.get("source_url"), str):
                return []
            if not isinstance(output.get("plan_items"), list) or not output["plan_items"]:
                return []
            canonical = canonical_json(dict(output))
            plan = dict(output)
            plan["content_hash"] = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()
            return [plan]

        # public_job_page, structured_job_details, job_search_results.
        return _extract_candidates(output)

    def _promote_one_locked(
        self,
        *,
        artifact_type: str,
        tool_name: str,
        raw: Mapping[str, Any],
    ) -> Artifact | None:
        """Validate, bound, dedup, and store one candidate (caller holds lock).

        Returns the stored artifact (new or dedup-hit), or ``None`` when the
        candidate fails the basic structural checks (missing source_url,
        invalid content_hash, empty content).
        """
        source_url = raw.get("source_url")
        content_hash = raw.get("content_hash")
        if artifact_type == "job_matching_report":
            # Derived deliverable: an empty-match report (migration plan
            # §6.5, no_candidate_satisfied_constraints) legitimately carries
            # no page URL.  Any present URL must still be a non-empty string.
            if source_url is not None and (
                not isinstance(source_url, str) or not source_url.strip()
            ):
                return None
            if isinstance(source_url, str):
                source_url = source_url[:2_048]
        else:
            if not isinstance(source_url, str) or not source_url.strip():
                return None
            source_url = source_url[:2_048]
        if not isinstance(content_hash, str) or len(content_hash) != 64:
            return None

        bounded = bound_content(raw)
        if not bounded:
            return None

        quality = self._quality_for(artifact_type, bounded)

        dedup_key = (artifact_type, source_url, content_hash)
        existing_id = self._dedup.get(dedup_key)
        if existing_id is not None:
            return self._by_id[existing_id]

        artifact_id = uuid.uuid4().hex
        # Import inside function to avoid circular-import fragility when the
        # contracts module definition order shifts.
        from ..contracts import Artifact  # noqa: WPS433

        artifact = Artifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            tool_name=tool_name,
            source_url=source_url,
            content_hash=content_hash,
            quality=quality,
            content=bounded,
        )
        self._artifacts.append(artifact)
        self._by_id[artifact_id] = artifact
        self._dedup[dedup_key] = artifact_id
        return artifact

    @staticmethod
    def _quality_for(artifact_type: str, content: dict[str, Any]) -> str:
        """Derive a quality tag from *content* for the given artifact type.

        For ``public_job_page`` we surface the ``quality`` field directly
        (``jd_complete`` / ``list_only`` / ``js_shell`` / ``empty``).
        For everything else we use ``job_bearing`` when the semantic
        validator passes, ``low_quality`` otherwise.
        """
        if artifact_type == "public_job_page":
            quality_val = content.get("quality")
            if isinstance(quality_val, str) and quality_val.strip():
                return quality_val
            return "unknown"
        if artifact_type == "structured_job_details":
            return "job_bearing" if _has_real_structured_candidate(content) else "low_quality"
        if artifact_type == "job_matching_report":
            return "job_bearing" if _validate_job_matching_report(content) else "low_quality"
        if artifact_type == "resume_tailoring_brief":
            return "job_bearing" if _validate_resume_tailoring_brief(content) else "low_quality"
        return "job_bearing"


__all__ = [
    "EvidenceStore",
    "TOOL_ARTIFACT_TYPE",
    "bound_content",
    "canonical_json",
    "content_hash_of",
    "_NAV_LABEL_TITLES",
    "_MIN_JD_BODY_CHARS",
    "_is_plausible_job_title",
    "_has_real_structured_candidate",
    "_is_quality_job_bearing",
]
