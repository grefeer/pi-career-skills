"""Evaluation compare tests — merge, audit fail-closed, regression flagging.

Covers: status tally, regression error_code flagging, external_blocked
separation, validate_record fail-closed exit, and comparison.md rendering.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pi_career_skills.evaluation.compare import (
    compare_pi_to_source,
    merge_results,
    render_comparison_md,
)
from pi_career_skills.evaluation.schema import validate_record

# ======================================================================
# Helpers — build valid minimal records
# ======================================================================


def _make_record(
    qid: str,
    status: str = "succeeded",
    error_code: str | None = None,
    artifacts: list[dict] | None = None,
    audit_status: str = "not_applicable",
) -> dict[str, Any]:
    """Build a minimal valid EvalRecord dict."""
    return {
        "schema_version": "pi_eval_record_v1",
        "id": qid,
        "type": "single",
        "question": f"test question {qid}",
        "meta": {"skills": ["job-discovery"]},
        "runtime": {"name": "pi-career-skills", "version": "0.1.0"},
        "model": {"id": "faux", "provider": "faux"},
        "config": {
            "prompt_hashes": {"supervisor": "abc", "job-discovery": "def"},
            "feature_flags": {},
            "seeded_urls": [],
        },
        "result": {
            "status": status,
            "error_code": error_code,
            "summary": "test summary",
        },
        "attempts": [
            {
                "attempt_id": "att-1",
                "status": status,
                "error_code": error_code,
                "summary": "test",
                "tool_calls": 1,
                "events": [],
            }
        ],
        "artifacts": artifacts or [],
        "events": [],
        "budget": {
            "limits": {
                "agent_turns": 100,
                "tool_calls": 200,
                "model_requests": 500,
                "input_tokens": 2000000,
                "wall_clock_seconds": 600,
                "auto_recoveries": 2,
            },
            "consumed": {
                "agent_turns": 5,
                "tool_calls": 10,
                "model_requests": 5,
                "input_tokens": 1000,
                "wall_clock_seconds": 1.5,
                "auto_recoveries": 0,
            },
        },
        "audit": {
            "status": audit_status,
            "checks": {"discovery": {"status": audit_status}},
        },
        "wall_seconds": 1.5,
    }


def _write_record(out_dir: Path, record: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{record['id']}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ======================================================================
# 1. merge_results: valid records load and tally
# ======================================================================


def test_merge_results_status_tally(tmp_path: Path) -> None:
    rec1 = _make_record("Q001", status="succeeded")
    rec2 = _make_record("Q002", status="waiting_user", error_code="no_progress")
    rec3 = _make_record("Q003", status="failed", error_code="runtime_error")

    for r in [rec1, rec2, rec3]:
        _write_record(tmp_path, r)

    records = merge_results(tmp_path)
    assert len(records) == 3

    # Tally via compare function.
    result = compare_pi_to_source(records)
    tally = result["pi_tally"]
    assert tally["succeeded"] == 1
    assert tally["waiting_user"] == 1
    assert tally["failed"] == 1


# ======================================================================
# 2. Regression error code flagged
# ======================================================================


def test_regression_error_code_flagged(tmp_path: Path) -> None:
    from pi_career_skills.evaluation.audit import is_regression_error_code

    regression_codes = [
        "invalid_tool_input",
        "invalid_tool_output",
        "unknown_tool",
        "tool_skill_forbidden",
        "contract_or_policy_error",
        "model_api_key_missing",
    ]
    for code in regression_codes:
        assert is_regression_error_code(code), f"{code} should be regression"

    rec = _make_record("Q001", status="failed", error_code="invalid_tool_input")
    _write_record(tmp_path, rec)

    records = merge_results(tmp_path)
    result = compare_pi_to_source(records)

    assert result["regression_count"] == 1
    assert result["regressions"][0]["id"] == "Q001"
    assert result["regressions"][0]["reason"] == "regression_error_code"


# ======================================================================
# 3. external_blocked NOT counted as regression
# ======================================================================


def test_external_blocked_separate_from_regression(tmp_path: Path) -> None:
    from pi_career_skills.evaluation.audit import is_regression_error_code

    blocked_codes = [
        "login_required",
        "captcha",
        "anti_bot",
        "sheet_rate_limited",
        "sheet_call_failed",
    ]
    for code in blocked_codes:
        assert not is_regression_error_code(
            code
        ), f"{code} should NOT be regression"

    rec = _make_record("Q001", status="waiting_user", error_code="login_required")
    _write_record(tmp_path, rec)

    records = merge_results(tmp_path)
    result = compare_pi_to_source(records)

    assert result["regression_count"] == 0
    assert result["external_blocked_count"] == 1
    assert result["external_blocked"][0]["error_code"] == "login_required"


# ======================================================================
# 4. validate_record fail-closed: bad record raises ValueError
# ======================================================================


def test_merge_results_fail_closed_on_schema_error(tmp_path: Path) -> None:
    # Write a malformed record (missing required field 'question').
    bad_record = {
        "schema_version": "pi_eval_record_v1",
        "id": "Q001",
        "type": "single",
        # missing 'question'
        "meta": {},
        "runtime": {"name": "pi-career-skills", "version": "0.1.0"},
        "model": {"id": "faux", "provider": "faux"},
        "config": {"prompt_hashes": {}, "feature_flags": {}, "seeded_urls": []},
        "result": {"status": "succeeded", "error_code": None, "summary": "x"},
        "attempts": [],
        "artifacts": [],
        "events": [],
        "budget": {"limits": {}, "consumed": {}},
        "audit": {},
        "wall_seconds": 0.0,
    }
    _write_record(tmp_path, bad_record)

    with pytest.raises(ValueError, match="Schema validation"):
        merge_results(tmp_path)

    # Also confirm validate_record raises on this record.
    with pytest.raises(ValueError):
        validate_record(bad_record)


# ======================================================================
# 5. launch_manifest.json and summary.json are skipped
# ======================================================================


def test_skip_manifest_and_summary_files(tmp_path: Path) -> None:
    rec = _make_record("Q001")
    _write_record(tmp_path, rec)

    # Add extra JSON files that should be skipped.
    (tmp_path / "launch_manifest.json").write_text(
        json.dumps({"total_ids": 1}), encoding="utf-8"
    )
    (tmp_path / "summary.json").write_text(
        json.dumps([{"id": "Q001"}]), encoding="utf-8"
    )

    records = merge_results(tmp_path)
    assert len(records) == 1
    assert records[0]["id"] == "Q001"


# ======================================================================
# 6. render_comparison_md produces valid markdown
# ======================================================================


def test_render_comparison_md() -> None:
    records = [
        _make_record("Q001", status="succeeded"),
        _make_record("Q002", status="waiting_user", error_code="no_progress"),
        _make_record("Q003", status="failed", error_code="invalid_tool_input"),
        _make_record("Q004", status="waiting_user", error_code="captcha"),
    ]
    result = compare_pi_to_source(records)
    md = render_comparison_md(result)

    assert "# Evaluation Comparison Report" in md
    assert "Status Tally" in md
    assert "Regression Summary" in md
    assert "Per-Question Diff" in md
    assert "invalid_tool_input" in md
    assert "captcha" in md
    # External blocked is in its own section.
    assert "External Blocked" in md


# ======================================================================
# 7. Missing source baselines noted, not crash
# ======================================================================


def test_missing_source_baselines_noted(tmp_path: Path) -> None:
    rec = _make_record("Q001", status="succeeded")
    _write_record(tmp_path, rec)

    # Source dir with no files for this id.
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    # Write a different id to source.
    (source_dir / "Q999.json").write_text(
        json.dumps(_make_record("Q999")), encoding="utf-8"
    )

    records = merge_results(tmp_path)
    result = compare_pi_to_source(records, source_nonchain_dir=source_dir)

    # Q001 should have missing source status.
    q001_entry = next(p for p in result["per_question"] if p["id"] == "Q001")
    assert q001_entry["source_status"] == "missing"
    assert "Q001" in result["missing_source_ids"]


# ======================================================================
# 8. completion_evidence_unavailable with qualified artifact = regression
# ======================================================================


def test_completion_evidence_unavailable_with_qualified_artifact_is_regression(
    tmp_path: Path,
) -> None:
    # Record with an artifact that would pass discovery audit but
    # error_code is completion_evidence_unavailable.
    artifact = {
        "artifact_id": "art-1",
        "artifact_type": "public_job_page",
        "source_url": "https://example.com/job/1",
        "content_hash": "a" * 64,
        "quality": "jd_complete",
        "content_json": {"visible_text": "JD text here"},
    }
    rec = _make_record(
        "Q001",
        status="waiting_user",
        error_code="completion_evidence_unavailable",
        artifacts=[artifact],
        audit_status="passed",
    )
    _write_record(tmp_path, rec)

    records = merge_results(tmp_path)
    result = compare_pi_to_source(records)

    # This counts as a regression.
    assert result["regression_count"] == 1
    assert (
        result["regressions"][0]["reason"]
        == "evidence_unavailable_with_qualified_artifact"
    )


# ======================================================================
# 9. per_question includes all records
# ======================================================================


def test_per_question_includes_all(tmp_path: Path) -> None:
    rec1 = _make_record("Q001", status="succeeded")
    rec2 = _make_record("Q002", status="failed", error_code="runtime_error")
    _write_record(tmp_path, rec1)
    _write_record(tmp_path, rec2)

    records = merge_results(tmp_path)
    result = compare_pi_to_source(records)

    assert len(result["per_question"]) == 2
    ids = {pq["id"] for pq in result["per_question"]}
    assert ids == {"Q001", "Q002"}


# ======================================================================
# 10. Chain record merges and validates
# ======================================================================


def test_merge_chain_record(tmp_path: Path) -> None:
    chain_rec = {
        "schema_version": "pi_eval_record_v1",
        "id": "C001",
        "type": "chain",
        "chain_length": 2,
        "links": [
            {
                "id": "C001-L1",
                "question": "find jobs",
                "meta": {},
                "config": {
                    "prompt_hashes": {},
                    "feature_flags": {},
                    "seeded_urls": [],
                },
                "result": {
                    "status": "succeeded",
                    "error_code": None,
                    "summary": "found",
                },
                "attempts": [],
                "artifacts": [],
                "events": [],
                "budget": {"limits": {}, "consumed": {}},
                "audit": {"status": "passed", "checks": {}},
                "wall_seconds": 1.0,
            },
            {
                "id": "C001-L2",
                "question": "match jobs",
                "meta": {},
                "config": {
                    "prompt_hashes": {},
                    "feature_flags": {},
                    "seeded_urls": [],
                },
                "result": {
                    "status": "succeeded",
                    "error_code": None,
                    "summary": "matched",
                },
                "attempts": [],
                "artifacts": [],
                "events": [],
                "budget": {"limits": {}, "consumed": {}},
                "audit": {"status": "passed", "checks": {}},
                "wall_seconds": 1.0,
            },
        ],
        "result": {
            "status": "succeeded",
            "error_code": None,
            "summary": "all done",
        },
        "audit": {"status": "passed", "links": []},
        "wall_seconds": 2.0,
    }
    _write_record(tmp_path, chain_rec)

    records = merge_results(tmp_path)
    assert len(records) == 1
    assert records[0]["type"] == "chain"
    assert records[0]["chain_length"] == 2
