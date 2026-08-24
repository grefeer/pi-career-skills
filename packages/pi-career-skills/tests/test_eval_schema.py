"""Tests for evaluation.schema — pi_eval_record_v1 schema + validators."""

from __future__ import annotations

import hashlib

import pytest

from pi_career_skills.evaluation.schema import (
    EvalArtifact,
    EvalChainRecord,
    EvalRecord,
    validate_record,
    validate_records,
)


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _valid_record(**overrides: object) -> dict:
    base = {
        "schema_version": "pi_eval_record_v1",
        "id": "Q011",
        "type": "single",
        "question": "test question",
        "meta": {"complexity": "simple"},
        "runtime": {"name": "pi-career-skills", "version": "0.1.0"},
        "model": {"id": "deepseek-v4-flash", "provider": "deepseek"},
        "config": {
            "prompt_hashes": {"supervisor": _sha256_hex("sup")},
            "feature_flags": {},
            "seeded_urls": ["https://example.com"],
        },
        "result": {"status": "succeeded", "error_code": None, "summary": "ok"},
        "attempts": [],
        "artifacts": [],
        "events": [],
        "budget": {
            "limits": {"agent_turns": 20, "tool_calls": 50},
            "consumed": {"agent_turns": 5, "wall_clock_seconds": 1.5},
        },
        "audit": {"status": "passed"},
        "wall_seconds": 1.5,
    }
    base.update(overrides)
    return base


def _valid_chain_record(**overrides: object) -> dict:
    link = {
        "id": "C001-L1",
        "question": "link q",
        "meta": {},
        "config": {"prompt_hashes": {}, "feature_flags": {}, "seeded_urls": []},
        "result": {"status": "succeeded", "error_code": None, "summary": "ok"},
        "attempts": [],
        "artifacts": [],
        "events": [],
        "budget": {"limits": {}, "consumed": {}},
        "audit": {"status": "passed"},
        "wall_seconds": 0.5,
    }
    base = {
        "schema_version": "pi_eval_record_v1",
        "id": "C001",
        "type": "chain",
        "chain_length": 1,
        "links": [link],
        "result": {"status": "succeeded", "error_code": None, "summary": "chain ok"},
        "audit": {"status": "passed"},
        "wall_seconds": 0.5,
    }
    base.update(overrides)
    return base


# ====================================================================
# Valid records
# ====================================================================


def test_valid_single_record_validates() -> None:
    rec = _valid_record()
    validate_record(rec)  # should not raise
    model = EvalRecord.model_validate(rec)
    assert model.id == "Q011"
    assert model.type == "single"
    assert model.result.status == "succeeded"


def test_valid_chain_record_validates() -> None:
    rec = _valid_chain_record()
    validate_record(rec)  # should not raise
    model = EvalChainRecord.model_validate(rec)
    assert model.id == "C001"
    assert model.type == "chain"
    assert model.chain_length == 1
    assert len(model.links) == 1


def test_validate_records_all_valid_returns_empty() -> None:
    errors = validate_records([_valid_record(), _valid_chain_record()])
    assert errors == []


# ====================================================================
# Missing required fields
# ====================================================================


def test_missing_id_raises_named_field() -> None:
    rec = _valid_record()
    del rec["id"]
    with pytest.raises(ValueError, match="id"):
        validate_record(rec)


def test_missing_result_raises_named_field() -> None:
    rec = _valid_record()
    del rec["result"]
    with pytest.raises(ValueError, match="result"):
        validate_record(rec)


def test_wrong_result_status_raises() -> None:
    rec = _valid_record(result={"status": "bogus"})
    with pytest.raises(ValueError):
        validate_record(rec)


def test_extra_field_raises_forbid() -> None:
    rec = _valid_record()
    rec["unexpected_field"] = 42
    with pytest.raises(ValueError, match="unexpected_field"):
        validate_record(rec)


def test_chain_length_mismatch_raises() -> None:
    rec = _valid_chain_record(chain_length=3)
    with pytest.raises(ValueError, match="chain_length"):
        validate_record(rec)


def test_non_dict_record_raises() -> None:
    with pytest.raises(ValueError, match="record must be a dict"):
        validate_record("not a dict")  # type: ignore[arg-type]


# ====================================================================
# validate_records — per-record errors
# ====================================================================


def test_validate_records_returns_one_per_bad() -> None:
    good = _valid_record()
    bad1 = _valid_record()
    del bad1["id"]
    bad2 = _valid_record()
    del bad2["result"]
    errors = validate_records([good, bad1, bad2])
    assert len(errors) == 2
    assert errors[0].startswith("record[1]:")
    assert errors[1].startswith("record[2]:")


# ====================================================================
# Bounded content on artifact
# ====================================================================


def test_artifact_content_bounded() -> None:
    long_str = "x" * 20_000
    art = EvalArtifact(
        artifact_id="a1",
        artifact_type="public_job_page",
        content_json={"long": long_str, "nested": {"deep": long_str}},
    )
    assert len(art.content_json["long"]) == 12_000


def test_artifact_content_list_bounded() -> None:
    big_list = list(range(30))
    art = EvalArtifact(
        artifact_id="a1",
        artifact_type="public_job_page",
        content_json={"items": big_list},
    )
    assert len(art.content_json["items"]) == 20


# ====================================================================
# Event payload size bound
# ====================================================================


def test_event_payload_over_4096_raises() -> None:
    big = {"data": "x" * 5000}
    from pi_career_skills.evaluation.schema import EvalEvent

    with pytest.raises(ValueError, match="payload"):
        EvalEvent(type="test", payload=big)


def test_event_payload_small_ok() -> None:
    from pi_career_skills.evaluation.schema import EvalEvent

    ev = EvalEvent(type="test", payload={"key": "value"})
    assert ev.type == "test"
