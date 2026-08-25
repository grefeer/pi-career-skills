"""Tests for evaluation.audit — skill-aware audit rules."""

from __future__ import annotations

import hashlib

import pytest

from pi_career_skills.evaluation.audit import (
    audit_chain,
    audit_record,
    is_regression_error_code,
)


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _base_record(
    status: str = "succeeded",
    artifacts: list[dict] | None = None,
    **overrides: object,
) -> dict:
    rec: dict = {
        "id": "Q011",
        "type": "single",
        "result": {"status": status, "error_code": None, "summary": ""},
        "artifacts": artifacts or [],
    }
    rec.update(overrides)
    return rec


def _jd_artifact(
    artifact_id: str = "jd1",
    url: str = "https://example.com/jd",
    quality: str = "jd_complete",
    text: str = "Full JD text here",
) -> dict:
    return {
        "artifact_id": artifact_id,
        "artifact_type": "public_job_page",
        "source_url": url,
        "content_hash": _sha256_hex(url + text),
        "quality": quality,
        "content_json": {"visible_text": text, "company": "Acme"},
    }


def _planning_artifact(
    *,
    artifact_id: str = "plan1",
    target_artifact_id: str = "jd1",
    source_url: str = "https://example.com/jd",
    topics: list[str] | None = None,
) -> dict:
    """Build a complete, raw planning artifact for audit-only fixtures."""
    plan_topics = topics or ["Python", "RAG"]
    return {
        "artifact_id": artifact_id,
        "artifact_type": "career_preparation_plan",
        "source_url": source_url,
        "content_hash": _sha256_hex("planning:" + artifact_id),
        "quality": "job_bearing",
        "content_json": {
            "target_artifact_id": target_artifact_id,
            "resolved_target_artifact_id": target_artifact_id,
            "selected_target_reference": target_artifact_id,
            "source_url": source_url,
            "jd_topics": plan_topics,
            "actions": ["Prepare a concrete Python and RAG project example."],
            "schedule_assumption": "No deadline was supplied; use relative order.",
            "schedule": {
                "kind": "relative",
                "target_date": None,
                "target_date_provenance": None,
                "relative_window": "Complete P0 before P1.",
            },
            "plan_items": [
                {
                    "topic": topic,
                    "priority": "P0" if index == 0 else "P1",
                    "time_budget_hours": 2,
                    "due_date": None,
                    "relative_order": "first" if index == 0 else "then",
                    "completion_criteria": f"Explain a {topic} implementation decision.",
                    "review_checkpoint": f"Review the JD requirement for {topic}.",
                    "evidence_basis": f"The target JD explicitly requires {topic}.",
                }
                for index, topic in enumerate(plan_topics)
            ],
            "skill_gaps": [],
        },
    }


def _structured_jd_artifact(
    *,
    artifact_id: str = "structured-jd",
    candidate_id: str = "candidate-1",
    source_url: str = "https://example.com/structured-jd",
    text: str = "LLM engineer role requires Python, RAG, and LangChain implementation experience.",
) -> dict:
    """Build a valid persisted structured JD with a canonical candidate target."""
    return {
        "artifact_id": artifact_id,
        "artifact_type": "structured_job_details",
        "source_url": source_url,
        "content_hash": _sha256_hex("structured:" + artifact_id),
        "quality": "job_bearing",
        "content_json": {
            "candidates": [
                {
                    "candidate_id": candidate_id,
                    "artifact_id": artifact_id,
                    "source_artifact_id": "page-source-1",
                    "source_url": source_url,
                    "title": "LLM Application Engineer",
                    "full_text": text,
                    "responsibilities": "Build and evaluate production LLM application systems.",
                    "requirements": "Python and RAG production experience.",
                }
            ]
        },
    }


# ====================================================================
# is_regression_error_code
# ====================================================================


def test_is_regression_error_code_true_cases() -> None:
    for code in [
        "invalid_tool_input",
        "invalid_tool_output",
        "unknown_tool",
        "tool_skill_forbidden",
        "contract_or_policy_error",
        "model_api_key_missing",
    ]:
        assert is_regression_error_code(code) is True


def test_is_regression_error_code_false_cases() -> None:
    for code in [
        "search_empty",
        "login_required",
        "captcha",
        "budget_exhausted",
        "something_else",
    ]:
        assert is_regression_error_code(code) is False


# ====================================================================
# Discovery audit
# ====================================================================


def test_discovery_pass_with_complete_jd() -> None:
    rec = _base_record(artifacts=[_jd_artifact()])
    result = audit_record(rec)
    assert result["status"] == "passed"
    assert result["skill"] == "discovery"
    assert result["checks"]["discovery"]["status"] == "passed"
    assert result["checks"]["discovery"]["valid_public_job_pages"] == 1


def test_discovery_fail_forbidden_quality() -> None:
    rec = _base_record(
        artifacts=[_jd_artifact(quality="blocked", text="blocked page")]
    )
    result = audit_record(rec)
    assert result["checks"]["discovery"]["status"] == "failed"
    assert result["checks"]["discovery"]["reason"] == "forbidden_artifact_quality"
    assert "blocked" in result["checks"]["discovery"]["qualities"]


def test_discovery_inconclusive_no_complete_no_forbidden() -> None:
    """A public_job_page with no quality field and empty visible text → inconclusive."""
    rec = _base_record(artifacts=[{
        "artifact_id": "a1",
        "artifact_type": "public_job_page",
        "source_url": "https://example.com/shell",
        "content_hash": _sha256_hex("shell"),
        "quality": "js_shell",
        "content_json": {"visible_text": ""},
    }])
    result = audit_record(rec)
    assert result["checks"]["discovery"]["status"] == "inconclusive"
    assert result["checks"]["discovery"]["reason"] == "no_complete_public_job_page"


def test_discovery_not_applicable_when_not_succeeded() -> None:
    rec = _base_record(status="failed")
    result = audit_record(rec)
    assert result["status"] == "not_applicable"
    assert result["reason"] == "result_not_succeeded"


def test_discovery_inconclusive_empty_artifacts() -> None:
    rec = _base_record(artifacts=[])
    result = audit_record(rec)
    assert result["checks"]["discovery"]["status"] == "inconclusive"


def test_discovery_jd_missing_content_hash_fails_pass() -> None:
    art = _jd_artifact()
    art["content_hash"] = "not-a-sha256"
    rec = _base_record(artifacts=[art])
    result = audit_record(rec)
    assert result["checks"]["discovery"]["status"] == "inconclusive"


# ====================================================================
# Matching audit
# ====================================================================


def test_matching_pass_with_refs() -> None:
    jd = _jd_artifact("jd1")
    match_report = {
        "artifact_id": "m1",
        "artifact_type": "job_matching_report",
        "content_json": {
            "matches": [
                {"candidate_id": "c1", "score": 0.9},
                {"candidate_id": "c2", "score": 0.8},
            ],
            "input_refs": [
                {
                    "artifact_id": jd["artifact_id"],
                    "source_url": jd["source_url"],
                    "content_hash": jd["content_hash"],
                },
            ],
        },
    }
    rec = _base_record(artifacts=[jd, match_report])
    result = audit_record(rec)
    assert result["checks"]["matching"]["status"] == "passed"


def test_matching_fail_unresolved_ref() -> None:
    match_report = {
        "artifact_id": "m1",
        "artifact_type": "job_matching_report",
        "content_json": {
            "matches": [{"candidate_id": "c1", "score": 0.9}],
            "input_refs": [
                {
                    "artifact_id": "ghost_jd",
                    "source_url": "https://nope.com",
                    "content_hash": _sha256_hex("nope"),
                },
            ],
        },
    }
    rec = _base_record(artifacts=[match_report])
    result = audit_record(rec)
    assert result["checks"]["matching"]["status"] == "failed"
    assert result["checks"]["matching"]["reason"] == "matching_input_refs_unresolved"


def test_matching_pass_no_candidate_flag() -> None:
    match_report = {
        "artifact_id": "m1",
        "artifact_type": "job_matching_report",
        "content_json": {
            "matches": [],
            "no_candidate_satisfied_constraints": True,
            "input_refs": [],
        },
    }
    rec = _base_record(artifacts=[match_report])
    result = audit_record(rec)
    assert result["checks"]["matching"]["status"] == "passed"
    assert result["checks"]["matching"]["no_candidate_satisfied_constraints"] is True


def test_matching_pass_explicit_no_match_reason() -> None:
    """The current runtime schema records a bounded negative result by reason."""
    match_report = {
        "artifact_id": "m1",
        "artifact_type": "job_matching_report",
        "content_json": {
            "matches": [],
            "no_match_reason": "no_candidate_satisfied_constraints",
            "evaluated_candidate_count": 1,
            "input_refs": [],
        },
    }
    result = audit_record(_base_record(artifacts=[match_report]))
    assert result["checks"]["matching"]["status"] == "passed"
    assert result["checks"]["matching"]["no_candidate_satisfied_constraints"] is True


def test_matching_inconclusive_no_report() -> None:
    rec = _base_record(artifacts=[])
    result = audit_record(rec)
    assert result["checks"]["matching"]["status"] == "inconclusive"
    assert result["checks"]["matching"]["reason"] == "no_matching_report"


def test_matching_pass_with_inherited_refs() -> None:
    match_report = {
        "artifact_id": "m1",
        "artifact_type": "job_matching_report",
        "content_json": {
            "matches": [{"candidate_id": "c1", "score": 0.9}],
            "input_refs": [
                {
                    "artifact_id": "inherited_jd",
                    "source_url": "https://prev.com/jd",
                    "content_hash": _sha256_hex("prev"),
                },
            ],
        },
    }
    inherited = [
        {
            "artifact_id": "inherited_jd",
            "source_url": "https://prev.com/jd",
            "content_hash": _sha256_hex("prev"),
        },
    ]
    rec = _base_record(artifacts=[match_report])
    result = audit_record(rec, inherited_refs=inherited)
    assert result["checks"]["matching"]["status"] == "passed"


# ====================================================================
# Tailoring audit
# ====================================================================


def test_tailoring_pass() -> None:
    jd = _jd_artifact("jd1")
    brief = {
        "artifact_id": "b1",
        "artifact_type": "resume_tailoring_brief",
        "content_json": {
            "target_artifact_id": "jd1",
            "safe_actions": [
                {"fact_ref": "skills", "suggestion": "highlight Vue"},
                {"fact_ref": "experience", "suggestion": "quantify impact"},
            ],
        },
    }
    confirmed = {"skills": ["Vue"], "experience": ["2 years"]}
    rec = _base_record(artifacts=[jd, brief])
    result = audit_record(rec, confirmed_facts=confirmed)
    assert result["checks"]["tailoring"]["status"] == "passed"
    assert result["checks"]["tailoring"]["safe_actions_count"] == 2


def test_tailoring_fail_unconfirmed_fact_ref() -> None:
    jd = _jd_artifact("jd1")
    brief = {
        "artifact_id": "b1",
        "artifact_type": "resume_tailoring_brief",
        "content_json": {
            "target_artifact_id": "jd1",
            "safe_actions": [
                {"fact_ref": "made_up_skill", "suggestion": "lie about skill"},
            ],
        },
    }
    confirmed = {"skills": ["Vue"]}
    rec = _base_record(artifacts=[jd, brief])
    result = audit_record(rec, confirmed_facts=confirmed)
    assert result["checks"]["tailoring"]["status"] == "failed"
    assert result["checks"]["tailoring"]["reason"] == "tailoring_unconfirmed_fact_refs"
    assert "made_up_skill" in result["checks"]["tailoring"]["unconfirmed_fact_refs"]


def test_tailoring_fail_unresolvable_target() -> None:
    brief = {
        "artifact_id": "b1",
        "artifact_type": "resume_tailoring_brief",
        "content_json": {
            "target_artifact_id": "ghost_jd",
            "safe_actions": [
                {"fact_ref": "skills", "suggestion": "highlight Vue"},
            ],
        },
    }
    confirmed = {"skills": ["Vue"]}
    rec = _base_record(artifacts=[brief])
    result = audit_record(rec, confirmed_facts=confirmed)
    assert result["checks"]["tailoring"]["status"] == "failed"
    assert result["checks"]["tailoring"]["reason"] == "tailoring_target_unresolved"


def test_tailoring_accepts_unique_source_url_for_transient_candidate_id() -> None:
    jd = _jd_artifact("jd1")
    brief = {
        "artifact_id": "b1",
        "artifact_type": "resume_tailoring_brief",
        "content_json": {
            "target_artifact_id": "candidate-transient",
            "source_url": jd["source_url"],
            "safe_actions": [{"fact_ref": "skills", "suggestion": "highlight Vue"}],
        },
    }

    result = audit_record(
        _base_record(artifacts=[jd, brief]),
        confirmed_facts={"skills": ["Vue"]},
    )

    assert result["checks"]["tailoring"]["status"] == "passed"


def test_tailoring_inconclusive_no_brief() -> None:
    rec = _base_record(artifacts=[])
    result = audit_record(rec, confirmed_facts={})
    assert result["checks"]["tailoring"]["status"] == "inconclusive"


def test_tailoring_not_applicable_after_explicit_matching_no_match() -> None:
    report = {
        "artifact_id": "m-no-match",
        "artifact_type": "job_matching_report",
        "content_json": {
            "matches": [],
            "evaluated_candidate_count": 2,
            "no_match_reason": "no_candidate_satisfied_constraints",
        },
    }
    rec = _base_record(
        artifacts=[report], meta={"skills": ["resume-tailoring"]}
    )
    result = audit_record(rec, confirmed_facts={})
    assert result["checks"]["tailoring"] == {
        "status": "passed",
        "reason": "tailoring_not_applicable_no_match",
    }


def test_role_plan_audit_requires_envelope_source_match() -> None:
    content = {
        "target_artifact_id": "role:ai-agent",
        "resolved_target_artifact_id": "role:ai-agent",
        "source_url": "user_goal://role/ai-agent",
        "jd_topics": ["python"],
        "actions": ["准备案例"],
        "plan_items": [{"evidence_basis": "user-stated role/profile (no employer JD available)"}],
    }
    mismatched = {
        "artifact_id": "plan-1",
        "artifact_type": "career_preparation_plan",
        "source_url": "user_goal://role/other",
        "content_json": content,
    }
    result = audit_record(
        _base_record(artifacts=[mismatched], meta={"skills": ["career-planning"]})
    )
    assert result["checks"]["planning"]["reason"] == "role_plan_provenance_invalid"


# ====================================================================
# Career-planning audit
# ====================================================================


def test_planning_uses_inherited_jd_and_takes_precedence_over_discovery() -> None:
    """A planning link must not be credited merely because its parent found a JD."""
    jd = _jd_artifact(
        "inherited-jd",
        url="https://example.com/inherited-jd",
        text="The target JD requires Python, RAG, and production deployment experience.",
    )
    plan = _planning_artifact(
        target_artifact_id="inherited-jd",
        source_url="https://example.com/inherited-jd",
    )
    rec = _base_record(
        artifacts=[plan], meta={"skills": ["career-planning"]}
    )

    result = audit_record(rec, inherited_refs=[jd])

    assert result["status"] == "passed"
    assert result["skill"] == "planning"
    assert result["checks"]["planning"]["status"] == "passed"
    assert result["checks"]["discovery"]["status"] == "inconclusive"


def test_declared_planning_without_artifact_is_not_hidden_by_discovery() -> None:
    """Removing a plan from a successful planning link must leave it inconclusive."""
    jd = _jd_artifact(text="The target JD requires Python and RAG.")
    rec = _base_record(
        artifacts=[jd], meta={"skills": ["career-planning"]}
    )

    result = audit_record(rec)

    assert result["status"] == "inconclusive"
    assert result["skill"] == "planning"
    assert result["checks"]["planning"] == {
        "status": "inconclusive",
        "reason": "no_career_preparation_plan",
    }
    assert result["checks"]["discovery"]["status"] == "passed"


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("unresolved_target", "planning_target_unresolved"),
        ("noncanonical_target", "planning_target_not_canonical"),
        ("source_url_mismatch", "planning_source_url_mismatch"),
        ("unsupported_topic", "planning_topics_not_supported"),
        ("empty_actions", "planning_actions_missing"),
        ("empty_items", "planning_plan_items_missing"),
        ("zero_hours", "planning_item_hours_invalid"),
        ("empty_criteria", "planning_item_completion_criteria_missing"),
        ("empty_checkpoint", "planning_item_review_checkpoint_missing"),
        ("empty_evidence_basis", "planning_item_evidence_basis_missing"),
        ("undeclared_item_topic", "planning_item_topic_not_declared"),
        ("relative_due_date", "planning_relative_schedule_has_due_date"),
    ],
)
def test_planning_rejects_invalid_evidence_contract(
    case: str, expected_reason: str
) -> None:
    """Each listed mutation removes a fact required for an auditable plan."""
    jd = _jd_artifact(text="The target JD requires Python and RAG.")
    plan = _planning_artifact()
    content = plan["content_json"]
    items = content["plan_items"]

    if case == "unresolved_target":
        content["target_artifact_id"] = "missing-jd"
        content["resolved_target_artifact_id"] = "missing-jd"
    elif case == "noncanonical_target":
        content["resolved_target_artifact_id"] = "candidate-selector"
    elif case == "source_url_mismatch":
        content["source_url"] = "https://example.com/other-jd"
    elif case == "unsupported_topic":
        content["jd_topics"] = ["Kubernetes"]
        content["plan_items"][0]["topic"] = "Kubernetes"
    elif case == "empty_actions":
        content["actions"] = []
    elif case == "empty_items":
        content["plan_items"] = []
    elif case == "zero_hours":
        items[0]["time_budget_hours"] = 0
    elif case == "empty_criteria":
        items[0]["completion_criteria"] = ""
    elif case == "empty_checkpoint":
        items[0]["review_checkpoint"] = ""
    elif case == "empty_evidence_basis":
        items[0]["evidence_basis"] = ""
    elif case == "undeclared_item_topic":
        items[0]["topic"] = "Docker"
    elif case == "relative_due_date":
        items[0]["due_date"] = "2030-05-06"
    else:  # pragma: no cover - parametrization above is exhaustive
        raise AssertionError(f"unknown test case: {case}")

    result = audit_record(
        _base_record(
            artifacts=[jd, plan], meta={"skills": ["career-planning"]}
        )
    )

    assert result["status"] == "failed"
    assert result["skill"] == "planning"
    assert result["checks"]["planning"]["reason"] == expected_reason


def test_planning_never_uses_its_own_artifact_as_a_target_jd() -> None:
    """A plan-shaped artifact has a URL but is not evidence for its own target."""
    jd = _jd_artifact(text="The target JD requires Python and RAG.")
    plan = _planning_artifact(artifact_id="plan-as-target", target_artifact_id="plan-as-target")
    rec = _base_record(
        artifacts=[jd, plan], meta={"skills": ["career-planning"]}
    )

    result = audit_record(rec)

    assert result["status"] == "failed"
    assert result["checks"]["planning"]["reason"] == "planning_target_unresolved"


@pytest.mark.parametrize("case", ["quality", "hash", "visible_text"])
def test_planning_rejects_invalid_public_target_evidence(case: str) -> None:
    """A selector cannot turn a shell, bad hash, or empty page into a JD target."""
    jd = _jd_artifact(text="The target JD requires Python and RAG.")
    if case == "quality":
        jd["quality"] = "list_only"
    elif case == "hash":
        jd["content_hash"] = "not-a-sha256"
    elif case == "visible_text":
        jd["content_json"]["visible_text"] = ""
    else:  # pragma: no cover - parametrization above is exhaustive
        raise AssertionError(f"unknown test case: {case}")
    plan = _planning_artifact()

    result = audit_record(
        _base_record(artifacts=[jd, plan], meta={"skills": ["career-planning"]})
    )

    assert result["status"] == "failed"
    assert result["checks"]["planning"]["reason"] == "planning_target_invalid"


def test_planning_rejects_unknown_selector_instead_of_falling_back_to_raw_page() -> None:
    """A public page may use its canonical id or known alias, never an arbitrary selector."""
    jd = _jd_artifact(text="The target JD requires Python and RAG.")
    plan = _planning_artifact()
    plan["content_json"]["selected_target_reference"] = "model-invented-selector"

    result = audit_record(
        _base_record(artifacts=[jd, plan], meta={"skills": ["career-planning"]})
    )

    assert result["status"] == "failed"
    assert result["checks"]["planning"]["reason"] == (
        "planning_selected_target_reference_unresolved"
    )


def test_planning_accepts_canonical_public_source_url_alias() -> None:
    """The source URL is a documented alias for the same persisted page target."""
    jd = _jd_artifact(text="The target JD requires Python and RAG.")
    plan = _planning_artifact()
    plan["content_json"]["selected_target_reference"] = jd["source_url"]

    result = audit_record(
        _base_record(artifacts=[jd, plan], meta={"skills": ["career-planning"]})
    )

    assert result["status"] == "passed"


def test_planning_accepts_real_structured_candidate_selector() -> None:
    """A candidate id may select the canonical structured-JD artifact it belongs to."""
    structured = _structured_jd_artifact()
    plan = _planning_artifact(
        target_artifact_id="structured-jd",
        source_url="https://example.com/structured-jd",
    )
    plan["content_json"]["selected_target_reference"] = "candidate-1"

    result = audit_record(
        _base_record(
            artifacts=[structured, plan], meta={"skills": ["career-planning"]}
        )
    )

    assert result["status"] == "passed"
    assert result["checks"]["planning"]["target_artifact_id"] == "structured-jd"


def test_planning_accepts_same_url_artifact_id_from_inherited_link() -> None:
    """Re-projected chain artifacts may have different IDs for one page URL."""
    canonical = _jd_artifact(
        artifact_id="canonical-page",
        text="Full JD requires Python and RAG implementation experience.",
    )
    inherited_projection = _jd_artifact(
        artifact_id="inherited-page",
        text="Full JD requires Python and RAG implementation experience.",
    )
    plan = _planning_artifact(
        target_artifact_id="canonical-page",
        source_url=canonical["source_url"],
    )
    plan["content_json"]["selected_target_reference"] = "inherited-page"

    result = audit_record(
        _base_record(
            artifacts=[canonical, inherited_projection, plan],
            meta={"skills": ["career-planning"]},
        )
    )

    assert result["status"] == "passed"


def test_planning_accepts_apply_url_as_structured_candidate_provenance() -> None:
    """Real extraction output may carry the page URL in apply_url only."""
    structured = _structured_jd_artifact()
    candidate = structured["content_json"]["candidates"][0]
    candidate.pop("source_url")
    candidate["apply_url"] = structured["source_url"]
    plan = _planning_artifact(
        target_artifact_id="structured-jd",
        source_url=structured["source_url"],
    )
    plan["content_json"]["selected_target_reference"] = "candidate-1"

    result = audit_record(
        _base_record(
            artifacts=[structured, plan], meta={"skills": ["career-planning"]}
        )
    )

    assert result["status"] == "passed"


def test_planning_accepts_matching_apply_url_when_other_candidate_urls_are_stale() -> None:
    """A stale source_url must not hide a matching apply_url provenance."""
    structured = _structured_jd_artifact()
    candidate = structured["content_json"]["candidates"][0]
    candidate["source_url"] = "https://stale.example.test/old"
    candidate["page_source_url"] = "https://stale.example.test/old"
    candidate["apply_url"] = structured["source_url"]
    plan = _planning_artifact(
        target_artifact_id="structured-jd",
        source_url=structured["source_url"],
    )
    plan["content_json"]["selected_target_reference"] = "candidate-1"

    result = audit_record(
        _base_record(
            artifacts=[structured, plan], meta={"skills": ["career-planning"]}
        )
    )

    assert result["status"] == "passed"


def test_planning_rejects_structured_target_without_a_valid_candidate() -> None:
    """Structured evidence needs a real title plus substantive JD text, not a shell."""
    structured = _structured_jd_artifact(text="too short")
    structured["content_json"]["candidates"][0]["title"] = "招聘"
    structured["content_json"]["candidates"][0]["responsibilities"] = ""
    structured["content_json"]["candidates"][0]["requirements"] = ""
    plan = _planning_artifact(
        target_artifact_id="structured-jd",
        source_url="https://example.com/structured-jd",
    )
    plan["content_json"]["selected_target_reference"] = "candidate-1"

    result = audit_record(
        _base_record(
            artifacts=[structured, plan], meta={"skills": ["career-planning"]}
        )
    )

    assert result["status"] == "failed"
    assert result["checks"]["planning"]["reason"] == "planning_target_invalid"


@pytest.mark.parametrize(
    ("action", "expected_reason"),
    [
        (
            "Prepare a concise interview story without naming an evidence-backed skill.",
            "planning_actions_not_topic_grounded",
        ),
        (
            "Prepare a Python project story, then submit an application.",
            "planning_action_forbidden",
        ),
    ],
)
def test_planning_actions_must_be_topic_grounded_and_safe(
    action: str, expected_reason: str
) -> None:
    """Plan actions may prepare for an interview but cannot execute an application."""
    jd = _jd_artifact(text="The target JD requires Python and RAG.")
    plan = _planning_artifact()
    plan["content_json"]["actions"] = [action]

    result = audit_record(
        _base_record(artifacts=[jd, plan], meta={"skills": ["career-planning"]})
    )

    assert result["status"] == "failed"
    assert result["checks"]["planning"]["reason"] == expected_reason


def test_planning_item_evidence_basis_must_name_its_topic() -> None:
    """A generic evidence label cannot substantiate a concrete preparation item."""
    jd = _jd_artifact(text="The target JD requires Python and RAG.")
    plan = _planning_artifact()
    plan["content_json"]["plan_items"][0]["evidence_basis"] = (
        "This target was selected by the matching step."
    )

    result = audit_record(
        _base_record(artifacts=[jd, plan], meta={"skills": ["career-planning"]})
    )

    assert result["status"] == "failed"
    assert result["checks"]["planning"]["reason"] == (
        "planning_item_evidence_basis_not_topic_grounded"
    )


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("missing_assumption", "planning_schedule_assumption_missing"),
        ("missing_relative_window", "planning_relative_window_missing"),
        ("empty_relative_due_date", "planning_relative_schedule_has_due_date"),
        ("missing_relative_order", "planning_relative_order_missing"),
        ("invalid_target_date", "planning_schedule_target_date_invalid"),
        ("missing_target_date_provenance", "planning_schedule_target_date_provenance_invalid"),
        ("mismatched_target_due_date", "planning_target_date_due_date_mismatch"),
    ],
)
def test_planning_enforces_schedule_evidence_contract(
    case: str, expected_reason: str
) -> None:
    """Relative schedules stay date-free; absolute schedules require user provenance."""
    jd = _jd_artifact(text="The target JD requires Python and RAG.")
    plan = _planning_artifact()
    content = plan["content_json"]
    schedule = content["schedule"]
    items = content["plan_items"]

    if case == "missing_assumption":
        content["schedule_assumption"] = ""
    elif case == "missing_relative_window":
        schedule["relative_window"] = ""
    elif case == "empty_relative_due_date":
        items[0]["due_date"] = ""
    elif case == "missing_relative_order":
        items[0]["relative_order"] = None
    else:
        schedule.update(
            {
                "kind": "target_date",
                "target_date": "2030-05-06",
                "target_date_provenance": "user_supplied",
                "relative_window": None,
            }
        )
        for item in items:
            item["due_date"] = "2030-05-06"
            item["relative_order"] = None
        if case == "invalid_target_date":
            schedule["target_date"] = "2030-02-31"
            for item in items:
                item["due_date"] = "2030-02-31"
        elif case == "missing_target_date_provenance":
            schedule["target_date_provenance"] = None
        elif case == "mismatched_target_due_date":
            items[1]["due_date"] = "2030-05-07"
        else:  # pragma: no cover - parametrization above is exhaustive
            raise AssertionError(f"unknown test case: {case}")

    result = audit_record(
        _base_record(artifacts=[jd, plan], meta={"skills": ["career-planning"]})
    )

    assert result["status"] == "failed"
    assert result["checks"]["planning"]["reason"] == expected_reason


def test_planning_accepts_user_supplied_iso_target_date() -> None:
    """An explicit user date is valid only when every plan item carries it unchanged."""
    jd = _jd_artifact(text="The target JD requires Python and RAG.")
    plan = _planning_artifact()
    content = plan["content_json"]
    content["schedule"].update(
        {
            "kind": "target_date",
            "target_date": "2030-05-06",
            "target_date_provenance": "user_supplied",
            "relative_window": None,
        }
    )
    for item in content["plan_items"]:
        item["due_date"] = "2030-05-06"
        item["relative_order"] = None

    result = audit_record(
        _base_record(artifacts=[jd, plan], meta={"skills": ["career-planning"]})
    )

    assert result["status"] == "passed"


# ====================================================================
# Chain audit
# ====================================================================


def _make_link(
    link_id: str, status: str, artifacts: list[dict] | None = None
) -> dict:
    return {
        "id": link_id,
        "question": f"question for {link_id}",
        "meta": {},
        "config": {"prompt_hashes": {}, "feature_flags": {}, "seeded_urls": []},
        "result": {"status": status, "error_code": None, "summary": ""},
        "attempts": [],
        "artifacts": artifacts or [],
        "events": [],
        "budget": {"limits": {}, "consumed": {}},
        "audit": {"status": "passed"},
        "wall_seconds": 0.0,
    }


def test_chain_all_pass() -> None:
    jd = _jd_artifact("jd1")
    link1 = _make_link("C001-L1", "succeeded", [jd])
    chain = {
        "id": "C001",
        "type": "chain",
        "chain_length": 1,
        "links": [link1],
        "result": {"status": "succeeded", "summary": ""},
    }
    result = audit_chain(chain)
    assert result["status"] == "passed"
    assert len(result["links"]) == 1


def test_chain_one_failed_link() -> None:
    jd = _jd_artifact("jd1")
    link1 = _make_link("C001-L1", "succeeded", [jd])
    match_report = {
        "artifact_id": "m1",
        "artifact_type": "job_matching_report",
        "content_json": {
            "matches": [{"candidate_id": "c1"}],
            "input_refs": [{"artifact_id": "ghost", "source_url": "", "content_hash": ""}],
        },
    }
    link2 = _make_link("C001-L2", "succeeded", [jd, match_report])
    chain = {
        "id": "C001",
        "type": "chain",
        "chain_length": 2,
        "links": [link1, link2],
        "result": {"status": "succeeded", "summary": ""},
    }
    result = audit_chain(chain)
    assert result["status"] == "failed"
    assert result["reason"] == "link_failed"


def test_chain_one_inconclusive_link() -> None:
    link1 = _make_link("C001-L1", "succeeded", [])
    chain = {
        "id": "C001",
        "type": "chain",
        "chain_length": 1,
        "links": [link1],
        "result": {"status": "succeeded", "summary": ""},
    }
    result = audit_chain(chain)
    assert result["status"] == "failed"
    assert result["reason"] == "link_inconclusive"


def test_chain_inherits_refs_between_links() -> None:
    jd = _jd_artifact("jd1")
    link1 = _make_link("C001-L1", "succeeded", [jd])
    match_report = {
        "artifact_id": "m1",
        "artifact_type": "job_matching_report",
        "content_json": {
            "matches": [{"candidate_id": "c1", "score": 0.9}],
            "input_refs": [
                {
                    "artifact_id": "jd1",
                    "source_url": jd["source_url"],
                    "content_hash": jd["content_hash"],
                },
            ],
        },
    }
    link2 = _make_link("C001-L2", "succeeded", [match_report])
    chain = {
        "id": "C001",
        "type": "chain",
        "chain_length": 2,
        "links": [link1, link2],
        "result": {"status": "succeeded", "summary": ""},
    }
    result = audit_chain(chain)
    assert result["links"][1]["checks"]["matching"]["status"] == "passed"
