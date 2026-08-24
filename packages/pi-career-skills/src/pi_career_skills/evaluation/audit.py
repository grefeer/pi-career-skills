"""Skill-aware audit for pi_eval_record_v1 records.

Implements the §12.1 audit rules:

* **discovery** — at least one ``public_job_page`` artifact with id, URL,
  SHA-256 hash, visible text, and ``quality == "jd_complete"``.
* **matching** — a ``job_matching_report`` artifact whose input refs
  resolve to artifacts in the record (or inherited evidence).
* **tailoring** — a ``resume_tailoring_brief`` artifact with resolvable
  target and confirmed fact references for every action.
* **chain** — per-link audit; any succeeded link that is failed or
  inconclusive makes the top-level audit not ``passed``.

The discovery rule is ported verbatim from ``tests/question/eval_policy.py:
audit_success_record``.  Matching / tailoring / chain rules are port
inventions per migration plan §12.1.
"""

from __future__ import annotations

import re
from typing import Any

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Error codes that count as internal regressions (plan §12.2).
_REGRESSION_ERROR_CODES: frozenset[str] = frozenset({
    "invalid_tool_input",
    "invalid_tool_output",
    "unknown_tool",
    "tool_skill_forbidden",
    "contract_or_policy_error",
    "model_api_key_missing",
})

_FORBIDDEN_QUALITIES: frozenset[str] = frozenset({
    "list_only", "search_empty", "blocked", "empty",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_regression_error_code(code: str) -> bool:
    """Return True when *code* is in the regression error set (plan §12.2)."""
    return code in _REGRESSION_ERROR_CODES


def _result_status(record: dict[str, Any]) -> str:
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    status = result.get("status")
    return status if isinstance(status, str) else ""


def _artifact_list(record: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    return [a for a in artifacts if isinstance(a, dict)]


def _artifact_ref(artifact: dict[str, Any]) -> tuple[str, str, str]:
    """Return ``(artifact_id, source_url, content_hash)`` for ref matching."""
    return (
        str(artifact.get("artifact_id", "")),
        str(artifact.get("source_url", "") or ""),
        str(artifact.get("content_hash", "") or ""),
    )


def _build_ref_index(
    artifacts: list[dict[str, Any]],
    inherited_refs: list[dict] | None,
) -> set[tuple[str, str, str]]:
    """Build a set of ``(id, url, hash)`` tuples from artifacts + inherited."""
    index: set[tuple[str, str, str]] = set()
    for art in artifacts:
        index.add(_artifact_ref(art))
    if inherited_refs:
        for ref in inherited_refs:
            if not isinstance(ref, dict):
                continue
            index.add((
                str(ref.get("artifact_id", "")),
                str(ref.get("source_url", "") or ""),
                str(ref.get("content_hash", "") or ""),
            ))
    return index


# ---------------------------------------------------------------------------
# Skill-specific audit rules
# ---------------------------------------------------------------------------


def _audit_discovery(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    """Audit job-discovery artifacts — verbatim from eval_policy.audit_success_record.

    Returns a dict with ``status`` ∈ {passed, failed, inconclusive} and
    supplementary keys.
    """
    valid_pages = 0
    verified_negative_source_scans = 0
    forbidden: list[str] = []
    for artifact in artifacts:
        quality = artifact.get("quality")
        if quality in _FORBIDDEN_QUALITIES:
            forbidden.append(str(quality))
        if artifact.get("artifact_type") != "public_job_page":
            # negative source scan branch (eval_policy.py:75-94)
            content_json = artifact.get("content_json", {}) if isinstance(artifact.get("content_json"), dict) else {}
            if (
                artifact.get("artifact_type") == "job_search_results"
                and content_json.get("provider") == "juejin_official_search"
                and content_json.get("source_scope") == "juejin.cn"
                and content_json.get("coverage_complete") is True
                and isinstance(content_json.get("time_window_days"), int)
                and content_json["time_window_days"] > 0
                and isinstance(content_json.get("scanned_result_count"), int)
                and content_json["scanned_result_count"] >= 0
                and content_json.get("matched_result_count") == 0
                and content_json.get("terminal_reason") == "search_empty"
                and content_json.get("result_count") == 0
                and isinstance(artifact.get("source_url"), str)
                and artifact["source_url"].startswith(
                    "https://api.juejin.cn/search_api/v1/search"
                )
                and isinstance(artifact.get("content_hash"), str)
                and _SHA256_RE.fullmatch(artifact["content_hash"]) is not None
            ):
                verified_negative_source_scans += 1
            continue
        content_json = artifact.get("content_json", {}) if isinstance(artifact.get("content_json"), dict) else {}
        visible_text = content_json.get("visible_text", "")
        if (
            isinstance(artifact.get("artifact_id"), str)
            and artifact["artifact_id"]
            and isinstance(artifact.get("source_url"), str)
            and artifact["source_url"]
            and isinstance(artifact.get("content_hash"), str)
            and _SHA256_RE.fullmatch(artifact["content_hash"]) is not None
            and isinstance(visible_text, str)
            and visible_text.strip()
            and quality == "jd_complete"
        ):
            valid_pages += 1
    if valid_pages:
        return {"status": "passed", "valid_public_job_pages": valid_pages}
    if verified_negative_source_scans:
        return {
            "status": "passed",
            "verified_negative_source_scans": verified_negative_source_scans,
        }
    if forbidden:
        return {
            "status": "failed",
            "reason": "forbidden_artifact_quality",
            "qualities": sorted(set(forbidden)),
        }
    return {"status": "inconclusive", "reason": "no_complete_public_job_page"}


def _audit_matching(
    artifacts: list[dict[str, Any]],
    ref_index: set[tuple[str, str, str]],
) -> dict[str, Any]:
    """Audit job-matching artifacts.

    PASS iff at least one ``job_matching_report`` artifact has non-empty
    matches (or an explicit ``no_candidate_satisfied_constraints`` flag)
    AND every input ref in the report resolves to a known artifact
    (record artifacts or inherited evidence).
    """
    report_artifacts = [
        a for a in artifacts if a.get("artifact_type") == "job_matching_report"
    ]
    if not report_artifacts:
        return {"status": "inconclusive", "reason": "no_matching_report"}

    for report in report_artifacts:
        content = report.get("content_json", {}) if isinstance(report.get("content_json"), dict) else {}
        matches = content.get("matches", [])
        no_candidate = content.get("no_candidate_satisfied_constraints", False)
        has_results = bool(matches) or bool(no_candidate)
        if not has_results:
            continue

        # Check input refs resolve
        input_refs = content.get("input_refs", [])
        if not isinstance(input_refs, list):
            return {"status": "failed", "reason": "matching_input_refs_malformed"}

        all_resolve = True
        unresolved: list[str] = []
        for ref in input_refs:
            if not isinstance(ref, dict):
                all_resolve = False
                unresolved.append(str(ref))
                continue
            ref_tuple = (
                str(ref.get("artifact_id", "")),
                str(ref.get("source_url", "") or ""),
                str(ref.get("content_hash", "") or ""),
            )
            if ref_tuple not in ref_index:
                all_resolve = False
                unresolved.append(str(ref.get("artifact_id", "<unknown>")))

        if all_resolve:
            return {
                "status": "passed",
                "matches_count": len(matches) if isinstance(matches, list) else 0,
                "no_candidate_satisfied_constraints": bool(no_candidate),
            }
        return {
            "status": "failed",
            "reason": "matching_input_refs_unresolved",
            "unresolved": unresolved,
        }

    return {"status": "inconclusive", "reason": "no_matching_results"}


def _audit_tailoring(
    artifacts: list[dict[str, Any]],
    ref_index: set[tuple[str, str, str]],
    confirmed_facts: dict | None,
) -> dict[str, Any]:
    """Audit resume-tailoring artifacts.

    PASS iff at least one ``resume_tailoring_brief`` artifact has a
    resolvable target artifact id, non-empty ``safe_actions``, and every
    action's ``fact_ref`` cites a key present in *confirmed_facts*.
    """
    brief_artifacts = [
        a for a in artifacts if a.get("artifact_type") == "resume_tailoring_brief"
    ]
    if not brief_artifacts:
        return {"status": "inconclusive", "reason": "no_tailoring_brief"}

    confirmed_keys = set(confirmed_facts.keys()) if confirmed_facts else set()

    for brief in brief_artifacts:
        content = brief.get("content_json", {}) if isinstance(brief.get("content_json"), dict) else {}
        target_id = content.get("target_artifact_id", "")
        safe_actions = content.get("safe_actions", [])

        if not isinstance(safe_actions, list) or not safe_actions:
            continue

        # Check target resolves
        target_found = False
        for (aid, _url, _h) in ref_index:
            if aid and target_id and aid == target_id:
                target_found = True
                break
        if not target_found:
            return {
                "status": "failed",
                "reason": "tailoring_target_unresolved",
                "target_artifact_id": str(target_id),
            }

        # Check every fact_ref is in confirmed_facts
        unconfirmed: list[str] = []
        for action in safe_actions:
            if not isinstance(action, dict):
                continue
            fact_ref = action.get("fact_ref")
            if fact_ref is not None and fact_ref not in confirmed_keys:
                unconfirmed.append(str(fact_ref))

        if unconfirmed:
            return {
                "status": "failed",
                "reason": "tailoring_unconfirmed_fact_refs",
                "unconfirmed_fact_refs": sorted(set(unconfirmed)),
            }

        return {
            "status": "passed",
            "safe_actions_count": len(safe_actions),
        }

    return {"status": "inconclusive", "reason": "no_usable_tailoring_brief"}


# ---------------------------------------------------------------------------
# Top-level audit
# ---------------------------------------------------------------------------


def _detect_skill(artifacts: list[dict[str, Any]]) -> str | None:
    """Heuristic: determine which skill the record represents by artifact types."""
    types = {a.get("artifact_type") for a in artifacts}
    if "resume_tailoring_brief" in types:
        return "tailoring"
    if "job_matching_report" in types:
        return "matching"
    if "public_job_page" in types or "job_search_results" in types:
        return "discovery"
    return None


def audit_record(
    record: dict,
    *,
    inherited_refs: list[dict] | None = None,
    confirmed_facts: dict | None = None,
) -> dict:
    """Skill-aware audit of a single (non-chain) eval record.

    Args:
        record: Raw record dict (``pi_eval_record_v1``, type ``"single"``).
        inherited_refs: Optional list of inherited evidence reference dicts
            (each with ``artifact_id``, ``source_url``, ``content_hash``)
            used for matching/tailoring ref resolution across chain links.
        confirmed_facts: Optional dict of confirmed profile fact keys used
            to validate tailoring ``fact_ref`` values.

    Returns:
        Dict with ``status`` ∈ {passed, failed, inconclusive, not_applicable}
        and ``checks`` dict with per-skill results.
    """
    status = _result_status(record)
    if status != "succeeded":
        return {
            "status": "not_applicable",
            "reason": "result_not_succeeded",
            "checks": {},
        }

    artifacts = _artifact_list(record)
    ref_index = _build_ref_index(artifacts, inherited_refs)
    skill = _detect_skill(artifacts)

    checks: dict[str, Any] = {}
    checks["discovery"] = _audit_discovery(artifacts)
    checks["matching"] = _audit_matching(artifacts, ref_index)
    checks["tailoring"] = _audit_tailoring(artifacts, ref_index, confirmed_facts)

    # Overall status: use the detected skill's result
    if skill is None:
        overall_status = "inconclusive"
        overall_reason = "no_skill_artifacts"
    else:
        overall_status = checks[skill]["status"]
        overall_reason = checks[skill].get("reason")

    result: dict[str, Any] = {
        "status": overall_status,
        "skill": skill,
        "checks": checks,
    }
    if overall_reason:
        result["reason"] = overall_reason
    return result


def audit_chain(
    record: dict,
    *,
    confirmed_facts_by_link: list[dict] | None = None,
) -> dict:
    """Audit a chain eval record (§12.1 chain rule).

    Each link is audited individually.  The top-level status is ``passed``
    only if ALL succeeded links audit as passed or not_applicable (with at
    least one passed).  If any succeeded link is failed or inconclusive,
    the top-level status is ``failed``.

    Args:
        record: Raw chain record dict (type ``"chain"``).
        confirmed_facts_by_link: Optional list of confirmed-fact dicts,
            one per link (index-aligned with ``record["links"]``).

    Returns:
        Dict with top-level ``status`` and a ``links`` list of per-link
        audit results.
    """
    links = record.get("links", []) if isinstance(record.get("links"), list) else []
    per_link: list[dict[str, Any]] = []
    passed_count = 0
    has_failed = False
    has_inconclusive = False

    inherited: list[dict] = []

    for i, link in enumerate(links):
        if not isinstance(link, dict):
            per_link.append({"status": "not_applicable", "reason": "link_not_dict"})
            continue

        cfacts = (
            confirmed_facts_by_link[i]
            if confirmed_facts_by_link and i < len(confirmed_facts_by_link)
            else None
        )

        link_audit = audit_record(
            link,
            inherited_refs=inherited if inherited else None,
            confirmed_facts=cfacts,
        )
        per_link.append(link_audit)

        # Accumulate inherited refs from link artifacts for next link
        for art in _artifact_list(link):
            inherited.append({
                "artifact_id": art.get("artifact_id", ""),
                "source_url": art.get("source_url", ""),
                "content_hash": art.get("content_hash", ""),
            })

        link_status = link_audit["status"]
        if link_status == "passed":
            passed_count += 1
        elif link_status == "failed":
            has_failed = True
        elif link_status == "inconclusive":
            has_inconclusive = True

    if has_failed:
        top_status = "failed"
        top_reason = "link_failed"
    elif has_inconclusive:
        top_status = "failed"
        top_reason = "link_inconclusive"
    elif passed_count > 0:
        top_status = "passed"
        top_reason = None
    else:
        top_status = "not_applicable"
        top_reason = "no_passed_links"

    result: dict[str, Any] = {
        "status": top_status,
        "links": per_link,
    }
    if top_reason:
        result["reason"] = top_reason
    return result


__all__ = [
    "audit_chain",
    "audit_record",
    "is_regression_error_code",
]
