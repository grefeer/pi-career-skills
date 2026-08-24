"""Tests for evaluation.audit — skill-aware audit rules."""

from __future__ import annotations

import hashlib

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


def test_tailoring_inconclusive_no_brief() -> None:
    rec = _base_record(artifacts=[])
    result = audit_record(rec, confirmed_facts={})
    assert result["checks"]["tailoring"]["status"] == "inconclusive"


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
