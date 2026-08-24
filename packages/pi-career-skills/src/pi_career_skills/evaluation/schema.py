"""pi_eval_record_v1 schema — Pydantic v2 models and validators.

All models use ``extra="forbid"`` and snake_case field names matching the
``pi_eval_record_v1`` JSON contract (migration plan §10).  Bounded content
on artifacts reuses ``tool_adapter.bound_content`` semantics (40 keys /
12 000-char strings / 20-item lists / 1 200-char nested strings).
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..tool_adapter import bound_content

# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------


class EvalArtifact(BaseModel):
    """An evidence or deliverable artifact recorded in an eval record."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    artifact_type: str
    source_url: str | None = None
    content_hash: str | None = None
    quality: str | None = None
    content_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content_json", mode="before")
    @classmethod
    def _bound_content(cls, v: Any) -> dict[str, Any]:
        if not isinstance(v, dict):
            raise ValueError("content_json must be a dict")
        return bound_content(v)


class EvalEvent(BaseModel):
    """A single event entry with a bounded payload."""

    model_config = ConfigDict(extra="forbid")

    type: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload", mode="after")
    @classmethod
    def _payload_bounded(cls, v: dict[str, Any]) -> dict[str, Any]:
        serialized = json.dumps(v, ensure_ascii=False, sort_keys=True)
        if len(serialized.encode("utf-8")) > 4096:
            raise ValueError("payload exceeds 4096 bytes when serialized")
        return v


class EvalAttempt(BaseModel):
    """A single physical model/tool attempt within a run."""

    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    status: str
    error_code: str | None = None
    summary: str | None = None
    tool_calls: int | None = None
    events: list[EvalEvent] = Field(default_factory=list)


class EvalBudgetLimits(BaseModel):
    """Configured budget ceilings for a run."""

    model_config = ConfigDict(extra="forbid")

    agent_turns: int = 0
    tool_calls: int = 0
    model_requests: int = 0
    input_tokens: int = 0
    wall_clock_seconds: int = 0
    auto_recoveries: int = 0


class EvalBudgetConsumed(BaseModel):
    """Actually consumed budget counters for a run."""

    model_config = ConfigDict(extra="forbid")

    agent_turns: int = 0
    tool_calls: int = 0
    model_requests: int = 0
    input_tokens: int = 0
    wall_clock_seconds: float = 0.0
    auto_recoveries: int = 0


class EvalResult(BaseModel):
    """Terminal result of a run or chain link."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "waiting_user", "failed"]
    error_code: str | None = None
    summary: str | None = None


class EvalConfig(BaseModel):
    """Run configuration snapshot."""

    model_config = ConfigDict(extra="forbid")

    prompt_hashes: dict[str, str] = Field(default_factory=dict)
    feature_flags: dict[str, Any] = Field(default_factory=dict)
    seeded_urls: list[str] = Field(default_factory=list)


class EvalModel(BaseModel):
    """Model identification."""

    model_config = ConfigDict(extra="forbid")

    id: str
    provider: str


class EvalRuntime(BaseModel):
    """Runtime identification."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str


# ---------------------------------------------------------------------------
# Top-level records
# ---------------------------------------------------------------------------


class EvalRecord(BaseModel):
    """A single (non-chain) eval record — §10.1 of the migration plan."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "pi_eval_record_v1"
    id: str
    type: Literal["single"] = "single"
    question: str
    meta: dict[str, Any] = Field(default_factory=dict)
    runtime: EvalRuntime
    model: EvalModel
    config: EvalConfig
    result: EvalResult
    attempts: list[EvalAttempt] = Field(default_factory=list)
    artifacts: list[EvalArtifact] = Field(default_factory=list)
    events: list[EvalEvent] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    audit: dict[str, Any] = Field(default_factory=dict)
    wall_seconds: float = 0.0

    @field_validator("budget", mode="before")
    @classmethod
    def _budget_keys(cls, v: Any) -> dict[str, Any]:
        if not isinstance(v, dict):
            raise ValueError("budget must be a dict")
        for key in ("limits", "consumed"):
            if key in v and not isinstance(v[key], dict):
                raise ValueError(f"budget.{key} must be a dict")
        return v


class EvalChainLinkRecord(BaseModel):
    """One link inside an ``EvalChainRecord``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    meta: dict[str, Any] = Field(default_factory=dict)
    config: EvalConfig
    result: EvalResult
    attempts: list[EvalAttempt] = Field(default_factory=list)
    artifacts: list[EvalArtifact] = Field(default_factory=list)
    events: list[EvalEvent] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    audit: dict[str, Any] = Field(default_factory=dict)
    wall_seconds: float = 0.0

    @field_validator("budget", mode="before")
    @classmethod
    def _budget_keys(cls, v: Any) -> dict[str, Any]:
        if not isinstance(v, dict):
            raise ValueError("budget must be a dict")
        for key in ("limits", "consumed"):
            if key in v and not isinstance(v[key], dict):
                raise ValueError(f"budget.{key} must be a dict")
        return v


class EvalChainRecord(BaseModel):
    """A chain eval record — §10.2 of the migration plan."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "pi_eval_record_v1"
    id: str
    type: Literal["chain"] = "chain"
    chain_length: int
    links: list[EvalChainLinkRecord]
    result: EvalResult
    audit: dict[str, Any] = Field(default_factory=dict)
    wall_seconds: float = 0.0

    @model_validator(mode="after")
    def _chain_length_matches(self) -> EvalChainRecord:
        if self.chain_length != len(self.links):
            raise ValueError(
                f"chain_length ({self.chain_length}) does not match "
                f"number of links ({len(self.links)})"
            )
        return self


EvalRecordUnion = EvalRecord | EvalChainRecord


# ---------------------------------------------------------------------------
# Public validators
# ---------------------------------------------------------------------------


def validate_record(record: dict) -> None:
    """Validate a raw dict against the pi_eval_record_v1 schema.

    Args:
        record: Raw record dictionary (either single or chain).

    Raises:
        ValueError: When the record does not conform.  The message names
            the offending field or structural issue.
    """
    if not isinstance(record, dict):
        raise ValueError("record must be a dict")
    rec_type = record.get("type", "single")
    if rec_type == "chain":
        try:
            EvalChainRecord.model_validate(record)
        except Exception as exc:  # noqa: BLE001 — surface any validation error
            raise ValueError(str(exc)) from exc
    else:
        try:
            EvalRecord.model_validate(record)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(str(exc)) from exc


def validate_records(records: list[dict]) -> list[str]:
    """Validate a list of raw records and return per-record error strings.

    Args:
        records: List of raw record dictionaries.

    Returns:
        A list of human-readable error strings, one per record that fails.
        Empty list when all records are valid.
    """
    errors: list[str] = []
    for idx, record in enumerate(records):
        try:
            validate_record(record)
        except ValueError as exc:
            errors.append(f"record[{idx}]: {exc}")
    return errors


__all__ = [
    "EvalArtifact",
    "EvalAttempt",
    "EvalBudgetConsumed",
    "EvalBudgetLimits",
    "EvalChainLinkRecord",
    "EvalChainRecord",
    "EvalConfig",
    "EvalEvent",
    "EvalModel",
    "EvalRecord",
    "EvalRecordUnion",
    "EvalResult",
    "EvalRuntime",
    "validate_record",
    "validate_records",
]
