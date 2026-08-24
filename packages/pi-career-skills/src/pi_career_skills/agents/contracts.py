"""Typed contracts shared by the supervisor delegation boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..runtime.budgets import BudgetConsumed


class DelegationStatus(StrEnum):
    """Trusted status returned by one skill delegation."""

    SUCCESS = "success"
    PARTIAL = "partial"
    NEED_USER = "need_user"
    RETRYABLE = "retryable"
    BLOCKED = "blocked"
    FAILED = "failed"


class DelegationAction(StrEnum):
    """Deterministic action hint for the supervisor."""

    CONTINUE = "continue"
    REROUTE = "reroute"
    ASK_USER = "ask_user"
    RETRY = "retry"
    STOP = "stop"


_DEFAULT_ACTIONS: dict[DelegationStatus, DelegationAction] = {
    DelegationStatus.SUCCESS: DelegationAction.CONTINUE,
    DelegationStatus.PARTIAL: DelegationAction.REROUTE,
    DelegationStatus.NEED_USER: DelegationAction.ASK_USER,
    DelegationStatus.RETRYABLE: DelegationAction.RETRY,
    DelegationStatus.BLOCKED: DelegationAction.REROUTE,
    DelegationStatus.FAILED: DelegationAction.STOP,
}


@dataclass(frozen=True)
class ArtifactRef:
    """A safe pointer; it deliberately has no business-content field."""

    artifact_id: str
    source_url: str | None = None
    content_hash: str | None = None
    artifact_type: str | None = None

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise ValueError("artifact_id must be non-empty")

    def to_dict(self) -> dict[str, str]:
        values = {"artifact_id": self.artifact_id}
        for key in ("source_url", "content_hash", "artifact_type"):
            value = getattr(self, key)
            if value:
                values[key] = value
        return values


@dataclass(frozen=True)
class ExpectedOutput:
    artifact_type: str
    requires_deliverable: bool = True

    def __post_init__(self) -> None:
        if not self.artifact_type.strip():
            raise ValueError("expected_output.artifact_type must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "requires_deliverable": self.requires_deliverable,
        }


@dataclass(frozen=True)
class AgentTask:
    """Structured task passed from the supervisor to one skill agent."""

    objective: str
    input_refs: tuple[ArtifactRef, ...] = ()
    constraints: Mapping[str, Any] = field(default_factory=dict)
    expected_output: ExpectedOutput | None = None

    def __post_init__(self) -> None:
        objective = self.objective.strip()
        if not objective:
            raise ValueError("objective must be non-empty")
        object.__setattr__(self, "objective", objective)
        object.__setattr__(
            self,
            "input_refs",
            tuple(_coerce_artifact_ref(ref) for ref in self.input_refs),
        )
        if not isinstance(self.constraints, Mapping):
            raise ValueError("constraints must be an object")
        object.__setattr__(self, "constraints", dict(self.constraints))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "objective": self.objective,
            "input_refs": [ref.to_dict() for ref in self.input_refs],
            "constraints": dict(self.constraints),
        }
        if self.expected_output is not None:
            payload["expected_output"] = self.expected_output.to_dict()
        return payload


@dataclass(frozen=True)
class DelegationOutcome:
    """Bounded result of a trusted skill-agent execution."""

    skill: str
    status: DelegationStatus | str
    summary: str | None = None
    refs: tuple[ArtifactRef, ...] | list[ArtifactRef | Mapping[str, Any]] = ()
    error_code: str | None = None
    action: DelegationAction | str | None = None
    consumed_budget: BudgetConsumed | None = None

    def __post_init__(self) -> None:
        raw_status = str(self.status)
        legacy_status = {"succeeded": DelegationStatus.SUCCESS, "error": DelegationStatus.FAILED}
        try:
            status = (
                legacy_status[raw_status]
                if raw_status in legacy_status
                else DelegationStatus(raw_status)
            )
        except ValueError as exc:
            raise ValueError(f"unknown delegation status: {raw_status}") from exc
        object.__setattr__(self, "status", status)
        object.__setattr__(
            self,
            "refs",
            tuple(_coerce_artifact_ref(ref) for ref in self.refs),
        )
        action = (
            _DEFAULT_ACTIONS[status]
            if self.action is None
            else DelegationAction(self.action)
        )
        object.__setattr__(self, "action", action)

    def safe_refs(self) -> list[dict[str, str]]:
        return [ref.to_dict() for ref in self.refs]


def _coerce_artifact_ref(value: ArtifactRef | Mapping[str, Any]) -> ArtifactRef:
    if isinstance(value, ArtifactRef):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("input_refs entries must be objects")
    allowed = {"artifact_id", "source_url", "content_hash", "artifact_type"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown artifact ref fields: {sorted(unknown)}")
    try:
        return ArtifactRef(
            artifact_id=str(value["artifact_id"]),
            source_url=_optional_string(value.get("source_url")),
            content_hash=_optional_string(value.get("content_hash")),
            artifact_type=_optional_string(value.get("artifact_type")),
        )
    except KeyError as exc:
        raise ValueError("artifact ref requires artifact_id") from exc


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("artifact ref values must be strings")
    return value


def normalize_agent_task(params: Mapping[str, Any]) -> AgentTask:
    """Validate structured delegate arguments and bridge legacy ``task_goal``."""

    allowed = {"task_goal", "objective", "input_refs", "constraints", "expected_output"}
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(f"unknown task fields: {sorted(unknown)}")

    objective_value = params.get("objective")
    legacy_goal = params.get("task_goal")
    if objective_value is None:
        objective_value = legacy_goal
    elif legacy_goal is not None and objective_value != legacy_goal:
        raise ValueError("objective and task_goal must match when both are supplied")
    if not isinstance(objective_value, str) or not objective_value.strip():
        raise ValueError("objective must be a non-empty string")

    raw_refs = params.get("input_refs", ())
    if raw_refs is None:
        raw_refs = ()
    if not isinstance(raw_refs, (list, tuple)):
        raise ValueError("input_refs must be an array")

    raw_constraints = params.get("constraints", {})
    if raw_constraints is None:
        raw_constraints = {}
    if not isinstance(raw_constraints, Mapping):
        raise ValueError("constraints must be an object")

    expected = params.get("expected_output")
    if isinstance(expected, str):
        expected = ExpectedOutput(expected)
    elif expected is not None:
        if not isinstance(expected, Mapping):
            raise ValueError("expected_output must be a string or object")
        unknown_expected = set(expected) - {"artifact_type", "requires_deliverable"}
        if unknown_expected:
            raise ValueError(
                f"unknown expected_output fields: {sorted(unknown_expected)}"
            )
        requires_deliverable = expected.get("requires_deliverable", True)
        if not isinstance(requires_deliverable, bool):
            raise ValueError("requires_deliverable must be boolean")
        expected = ExpectedOutput(
            artifact_type=str(expected.get("artifact_type", "")),
            requires_deliverable=requires_deliverable,
        )

    return AgentTask(
        objective=objective_value,
        input_refs=tuple(_coerce_artifact_ref(ref) for ref in raw_refs),
        constraints=dict(raw_constraints),
        expected_output=expected,
    )


__all__ = [
    "AgentTask",
    "ArtifactRef",
    "DelegationAction",
    "DelegationOutcome",
    "DelegationStatus",
    "ExpectedOutput",
    "normalize_agent_task",
]
