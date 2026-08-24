"""Skill-aware audit for pi_eval_record_v1 records.

Implements the §12.1 audit rules:

* **discovery** — at least one ``public_job_page`` artifact with id, URL,
  SHA-256 hash, visible text, and ``quality == "jd_complete"``.
* **matching** — a ``job_matching_report`` artifact whose input refs
  resolve to artifacts in the record (or inherited evidence).
* **tailoring** — a ``resume_tailoring_brief`` artifact with resolvable
  target and confirmed fact references for every action.
* **planning** — a ``career_preparation_plan`` anchored to a canonical
  persisted JD with factually supported topics and reviewable plan items.
* **chain** — per-link audit; any succeeded link that is failed or
  inconclusive makes the top-level audit not ``passed``.

The discovery rule is ported verbatim from ``tests/question/eval_policy.py:
audit_success_record``.  Matching / tailoring / chain rules are port
inventions per migration plan §12.1.
"""

from __future__ import annotations

from datetime import date
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

# Only these artifacts can serve as canonical JD targets for tailoring or
# planning. Derived deliverables may carry a source URL, but must never become
# self-referential target evidence for a later skill.
_TARGET_EVIDENCE_ARTIFACT_TYPES: frozenset[str] = frozenset({
    "public_job_page", "structured_job_details",
})

_NAVIGATION_ONLY_TITLES: frozenset[str] = frozenset({
    "浏览职位",
    "查看全部",
    "申请职位",
    "职位",
    "岗位",
    "职位列表",
    "岗位列表",
    "首页",
    "招聘",
    "投递",
    "登录",
    "注册",
})

# Planning may prepare a user for an interview, but it must never execute an
# application, payment, purchase, transfer, deletion, resignation, or contract
# action.  Keep this narrow: generic words such as “application” are allowed
# in a JD topic; only action phrases are denied.
_FORBIDDEN_PLANNING_ACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bapply(?:\s+for)?\b", re.IGNORECASE),
    re.compile(r"\bsubmit\s+(?:an?\s+)?(?:application|resume|cv)\b", re.IGNORECASE),
    re.compile(r"\b(?:pay|payment|transfer|purchase|buy|delete|destroy|resign|quit)\b", re.IGNORECASE),
)
_FORBIDDEN_PLANNING_ACTION_PHRASES: tuple[str, ...] = (
    "投递简历",
    "提交申请",
    "支付",
    "付款",
    "转账",
    "购买",
    "下单",
    "删除",
    "清空",
    "辞职",
    "离职",
    "签署合同",
    "签约",
)


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


def _declared_skill(record: dict[str, Any]) -> str | None:
    """Return the most specific recognized skill declared by record metadata."""
    meta = record.get("meta")
    skills = meta.get("skills") if isinstance(meta, dict) else None
    if not isinstance(skills, list):
        return None
    declared = {skill for skill in skills if isinstance(skill, str)}
    for source_name, audit_name in (
        ("career-planning", "planning"),
        ("resume-tailoring", "tailoring"),
        ("job-matching", "matching"),
        ("job-discovery", "discovery"),
    ):
        if source_name in declared:
            return audit_name
    return None


def _target_evidence_by_id(
    artifacts: list[dict[str, Any]],
    inherited_refs: list[dict] | None,
) -> dict[str, dict[str, Any]]:
    """Index persisted candidate targets, retaining enough content to audit them.

    ``inherited_refs`` must contain complete serialized job artifacts, not
    shallow ``artifact_id/source_url/content_hash`` references: planning needs
    the inherited ``content_json`` to prove its topics came from a JD.
    """
    evidence: dict[str, dict[str, Any]] = {}
    for artifact in [*artifacts, *(inherited_refs or [])]:
        if not isinstance(artifact, dict):
            continue
        if artifact.get("artifact_type") not in _TARGET_EVIDENCE_ARTIFACT_TYPES:
            continue
        artifact_id = artifact.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id.strip():
            evidence.setdefault(artifact_id, artifact)
    return evidence


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _candidate_matches_reference(candidate: dict[str, Any], reference: str) -> bool:
    """Match the user/model selector to a structured-candidate identity."""
    return any(
        candidate.get(key) == reference
        for key in (
            "candidate_id",
            "artifact_id",
            "source_artifact_id",
            "source_url",
            "page_source_url",
        )
    )


def _candidate_text(candidate: dict[str, Any]) -> str:
    """Return the factual text fields available for one extracted candidate."""
    fields = (
        "full_text",
        "visible_text",
        "page_text_prefix",
        "title",
        "responsibilities",
        "requirements",
    )
    return "\n".join(
        value.strip()
        for field in fields
        if isinstance((value := candidate.get(field)), str) and value.strip()
    )


def _structured_candidates(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten candidates from one persisted structured-detail artifact."""
    content = artifact.get("content_json")
    if not isinstance(content, dict):
        return []
    candidates: list[dict[str, Any]] = []
    for key in ("candidates", "details"):
        raw = content.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            nested = item.get("candidates")
            if isinstance(nested, list):
                candidates.extend(candidate for candidate in nested if isinstance(candidate, dict))
            else:
                candidates.append(item)
    return candidates


def _has_valid_target_fields(artifact: dict[str, Any]) -> bool:
    """Check the persisted identity fields required for a canonical JD target."""
    return (
        _nonempty_string(artifact.get("artifact_id"))
        and _nonempty_string(artifact.get("source_url"))
        and isinstance(artifact.get("content_hash"), str)
        and _SHA256_RE.fullmatch(artifact["content_hash"]) is not None
    )


def _is_valid_structured_candidate(
    candidate: dict[str, Any], *, source_url: str
) -> bool:
    """Require a real role title, source provenance, and substantive JD body."""
    title = candidate.get("title")
    candidate_url = candidate.get("page_source_url") or candidate.get("source_url")
    if not (
        _nonempty_string(title)
        and title.strip() not in _NAVIGATION_ONLY_TITLES
        and candidate_url == source_url
    ):
        return False
    return len(_candidate_text(candidate)) >= 20


def _is_valid_target_evidence(artifact: dict[str, Any]) -> bool:
    """Return whether an artifact is a complete public page or structured JD."""
    if not _has_valid_target_fields(artifact):
        return False
    content = artifact.get("content_json")
    if not isinstance(content, dict):
        return False
    if artifact.get("artifact_type") == "public_job_page":
        return (
            artifact.get("quality") == "jd_complete"
            and _nonempty_string(content.get("visible_text"))
        )
    if artifact.get("artifact_type") == "structured_job_details":
        source_url = artifact.get("source_url")
        return any(
            _is_valid_structured_candidate(candidate, source_url=source_url)
            for candidate in _structured_candidates(artifact)
        )
    return False


def _canonical_selector_aliases(artifact: dict[str, Any]) -> set[str]:
    """Return safe aliases for one persisted canonical target artifact."""
    artifact_id = artifact.get("artifact_id")
    source_url = artifact.get("source_url")
    content_hash = artifact.get("content_hash")
    assert isinstance(artifact_id, str)
    assert isinstance(source_url, str)
    assert isinstance(content_hash, str)
    return {artifact_id, source_url, f"observed:{content_hash}"}


def _resolve_target_jd_text(
    artifact: dict[str, Any], selected_reference: str
) -> tuple[str | None, str | None]:
    """Resolve a canonical/real selector to JD text, never silently fallback.

    Returns ``(text, None)`` for a valid selector and
    ``(None, "planning_selected_target_reference_unresolved")`` otherwise.
    """
    aliases = _canonical_selector_aliases(artifact)
    artifact_type = artifact.get("artifact_type")
    content = artifact.get("content_json")
    assert isinstance(content, dict)

    if artifact_type == "public_job_page":
        if selected_reference not in aliases:
            return None, "planning_selected_target_reference_unresolved"
        visible_text = content.get("visible_text")
        assert isinstance(visible_text, str)
        return visible_text, None

    candidates = [
        candidate
        for candidate in _structured_candidates(artifact)
        if _is_valid_structured_candidate(candidate, source_url=artifact["source_url"])
    ]
    if selected_reference in aliases:
        return "\n".join(_candidate_text(candidate) for candidate in candidates), None
    selected = [
        candidate
        for candidate in candidates
        if _candidate_matches_reference(candidate, selected_reference)
    ]
    if len(selected) != 1:
        return None, "planning_selected_target_reference_unresolved"
    return _candidate_text(selected[0]), None


def _is_forbidden_planning_action(action: str) -> bool:
    """Recognize only documented irreversible/action-taking plan verbs."""
    return any(pattern.search(action) for pattern in _FORBIDDEN_PLANNING_ACTION_PATTERNS) or any(
        phrase in action for phrase in _FORBIDDEN_PLANNING_ACTION_PHRASES
    )


def _is_iso_date(value: object) -> bool:
    """Accept only canonical JSON ISO-8601 calendar dates."""
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _topic_supported_by_jd(topic: str, jd_text: str) -> bool:
    """Match ASCII skills as identifiers while preserving CJK phrase support."""
    if re.fullmatch(r"[A-Za-z0-9_]+", topic):
        return re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(topic)}(?![A-Za-z0-9_])",
            jd_text,
            flags=re.IGNORECASE,
        ) is not None
    return topic.casefold() in jd_text.casefold()


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


def _audit_planning(
    artifacts: list[dict[str, Any]],
    inherited_refs: list[dict] | None,
) -> dict[str, Any]:
    """Audit evidence-grounded ``career_preparation_plan`` artifacts.

    A plan is usable only when it points to a canonical stored JD, preserves
    the JD source URL, and derives every stated topic and action item from
    that JD.  This is deliberately stricter than discovery: a copied
    inherited page cannot turn a missing plan into a successful planning run.
    """
    plan_artifacts = [
        artifact
        for artifact in artifacts
        if artifact.get("artifact_type") == "career_preparation_plan"
    ]
    if not plan_artifacts:
        return {"status": "inconclusive", "reason": "no_career_preparation_plan"}

    target_evidence = _target_evidence_by_id(artifacts, inherited_refs)
    first_failure: dict[str, Any] | None = None

    for plan in plan_artifacts:
        content = plan.get("content_json")
        if not isinstance(content, dict):
            failure = {"status": "failed", "reason": "planning_content_malformed"}
        else:
            target_id = content.get("target_artifact_id")
            resolved_target_id = content.get("resolved_target_artifact_id")
            if not isinstance(target_id, str) or not target_id.strip():
                failure = {"status": "failed", "reason": "planning_target_missing"}
            elif (
                not isinstance(resolved_target_id, str)
                or not resolved_target_id.strip()
                or resolved_target_id != target_id
            ):
                failure = {
                    "status": "failed",
                    "reason": "planning_target_not_canonical",
                }
            else:
                target = target_evidence.get(target_id)
                if target is None:
                    failure = {
                        "status": "failed",
                        "reason": "planning_target_unresolved",
                        "target_artifact_id": target_id,
                    }
                elif not _is_valid_target_evidence(target):
                    failure = {
                        "status": "failed",
                        "reason": "planning_target_invalid",
                        "target_artifact_id": target_id,
                    }
                else:
                    target_url = target["source_url"]
                    plan_url = content.get("source_url")
                    artifact_url = plan.get("source_url")
                    if not (
                        plan_url == target_url
                        and artifact_url == target_url
                    ):
                        failure = {
                            "status": "failed",
                            "reason": "planning_source_url_mismatch",
                        }
                    else:
                        selected_reference = content.get("selected_target_reference")
                        if not isinstance(selected_reference, str) or not selected_reference.strip():
                            failure = {
                                "status": "failed",
                                "reason": "planning_selected_target_reference_missing",
                            }
                        else:
                            jd_text, selector_error = _resolve_target_jd_text(
                                target, selected_reference
                            )
                            if selector_error is not None:
                                failure = {
                                    "status": "failed",
                                    "reason": selector_error,
                                }
                            else:
                                topics = content.get("jd_topics")
                                if not (
                                    isinstance(topics, list)
                                    and topics
                                    and all(
                                        isinstance(topic, str) and topic.strip()
                                        for topic in topics
                                    )
                                ):
                                    failure = {
                                        "status": "failed",
                                        "reason": "planning_topics_missing",
                                    }
                                else:
                                    typed_topics = [str(topic) for topic in topics]
                                    unsupported_topics = [
                                        topic
                                        for topic in typed_topics
                                        if not _topic_supported_by_jd(topic, jd_text or "")
                                    ]
                                    if unsupported_topics:
                                        failure = {
                                            "status": "failed",
                                            "reason": "planning_topics_not_supported",
                                            "unsupported_topics": unsupported_topics,
                                        }
                                    else:
                                        failure = _audit_planning_actions_and_items(
                                            content,
                                            jd_text or "",
                                            typed_topics,
                                        )

        if failure is None:
            # The helper returns None only for a fully valid plan.
            return {
                "status": "passed",
                "target_artifact_id": content["target_artifact_id"],
                "topics_count": len(content["jd_topics"]),
                "actions_count": len(content["actions"]),
                "plan_items_count": len(content["plan_items"]),
            }
        if first_failure is None:
            first_failure = failure

    return first_failure or {
        "status": "inconclusive",
        "reason": "no_usable_career_preparation_plan",
    }


def _audit_planning_actions_and_items(
    content: dict[str, Any],
    jd_text: str,
    topics: list[str],
) -> dict[str, Any] | None:
    """Return a failure payload unless actions and plan items are reviewable."""
    actions = content.get("actions")
    if not (
        isinstance(actions, list)
        and actions
        and all(_nonempty_string(action) for action in actions)
    ):
        return {"status": "failed", "reason": "planning_actions_missing"}
    for action in actions:
        if _is_forbidden_planning_action(action):
            return {"status": "failed", "reason": "planning_action_forbidden"}
        if not any(_topic_supported_by_jd(topic, action) for topic in topics):
            return {
                "status": "failed",
                "reason": "planning_actions_not_topic_grounded",
            }

    items = content.get("plan_items")
    if not isinstance(items, list) or not items:
        return {"status": "failed", "reason": "planning_plan_items_missing"}

    if not _nonempty_string(content.get("schedule_assumption")):
        return {
            "status": "failed",
            "reason": "planning_schedule_assumption_missing",
        }

    schedule = content.get("schedule")
    if not isinstance(schedule, dict) or schedule.get("kind") not in {
        "relative",
        "target_date",
    }:
        return {"status": "failed", "reason": "planning_schedule_malformed"}

    schedule_kind = schedule["kind"]
    target_date = schedule.get("target_date")
    if schedule_kind == "relative":
        if not _nonempty_string(schedule.get("relative_window")):
            return {
                "status": "failed",
                "reason": "planning_relative_window_missing",
            }
        if target_date is not None:
            return {
                "status": "failed",
                "reason": "planning_relative_schedule_has_due_date",
            }
        if (
            "target_date_provenance" not in schedule
            or schedule.get("target_date_provenance") is not None
        ):
            return {
                "status": "failed",
                "reason": "planning_relative_target_date_provenance_invalid",
            }
    else:
        if not _is_iso_date(target_date):
            return {
                "status": "failed",
                "reason": "planning_schedule_target_date_invalid",
            }
        if schedule.get("target_date_provenance") != "user_supplied":
            return {
                "status": "failed",
                "reason": "planning_schedule_target_date_provenance_invalid",
            }

    topic_set = {topic.casefold() for topic in topics}
    for item in items:
        if not isinstance(item, dict):
            return {"status": "failed", "reason": "planning_plan_item_malformed"}
        item_topic = item.get("topic")
        if not _nonempty_string(item_topic):
            return {"status": "failed", "reason": "planning_item_topic_missing"}
        if item_topic.casefold() not in topic_set:
            return {"status": "failed", "reason": "planning_item_topic_not_declared"}
        if not _topic_supported_by_jd(item_topic, jd_text):
            return {"status": "failed", "reason": "planning_item_topic_not_supported"}
        hours = item.get("time_budget_hours")
        if isinstance(hours, bool) or not isinstance(hours, int) or hours <= 0:
            return {"status": "failed", "reason": "planning_item_hours_invalid"}
        if not _nonempty_string(item.get("completion_criteria")):
            return {
                "status": "failed",
                "reason": "planning_item_completion_criteria_missing",
            }
        if not _nonempty_string(item.get("review_checkpoint")):
            return {
                "status": "failed",
                "reason": "planning_item_review_checkpoint_missing",
            }
        if not _nonempty_string(item.get("evidence_basis")):
            return {
                "status": "failed",
                "reason": "planning_item_evidence_basis_missing",
            }
        if not _topic_supported_by_jd(item_topic, item["evidence_basis"]):
            return {
                "status": "failed",
                "reason": "planning_item_evidence_basis_not_topic_grounded",
            }
        if schedule_kind == "relative" and item.get("due_date") is not None:
            return {
                "status": "failed",
                "reason": "planning_relative_schedule_has_due_date",
            }
        if schedule_kind == "relative" and item.get("relative_order") not in {
            "first",
            "then",
        }:
            return {
                "status": "failed",
                "reason": "planning_relative_order_missing",
            }
        if schedule_kind == "target_date" and item.get("due_date") != target_date:
            return {
                "status": "failed",
                "reason": "planning_target_date_due_date_mismatch",
            }
    return None


# ---------------------------------------------------------------------------
# Top-level audit
# ---------------------------------------------------------------------------


def _detect_skill(record: dict[str, Any], artifacts: list[dict[str, Any]]) -> str | None:
    """Determine the audited skill from deliverables, then declared intent.

    Planning must win before discovery because a planning link often carries
    inherited JD artifacts.  When a failed planning link has no plan artifact,
    its declared ``meta.skills`` still selects the planning audit rather than
    allowing an inherited page to hide the omission.
    """
    types = {a.get("artifact_type") for a in artifacts}
    if "career_preparation_plan" in types:
        return "planning"
    declared = _declared_skill(record)
    if declared == "planning":
        return "planning"
    if "resume_tailoring_brief" in types:
        return "tailoring"
    if "job_matching_report" in types:
        return "matching"
    if "public_job_page" in types or "job_search_results" in types:
        return "discovery"
    return declared


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
    skill = _detect_skill(record, artifacts)

    checks: dict[str, Any] = {}
    checks["discovery"] = _audit_discovery(artifacts)
    checks["matching"] = _audit_matching(artifacts, ref_index)
    checks["tailoring"] = _audit_tailoring(artifacts, ref_index, confirmed_facts)
    checks["planning"] = _audit_planning(artifacts, inherited_refs)

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

        # Carry only persisted JD evidence.  A matching/tailoring/planning
        # deliverable can contain a source URL but must never become a future
        # canonical target just because it was produced earlier in the chain.
        for art in _artifact_list(link):
            if art.get("artifact_type") in _TARGET_EVIDENCE_ARTIFACT_TYPES:
                inherited.append(art)

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
