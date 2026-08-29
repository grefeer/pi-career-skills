"""Typed contracts shared by the skill-delegation boundary.

Kept to the surface the deterministic pipeline actually consumes: the run
controller builds ``AgentTask`` from a bare objective string and reads back
``DelegationOutcome.status / error_code / summary``.  The LLM-supervisor-era
surface (``DelegationAction``, structured task fields, ``normalize_agent_task``)
was removed with the supervisor it served.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class DelegationStatus(StrEnum):
    """Trusted status returned by one skill delegation."""

    SUCCESS = "success"
    PARTIAL = "partial"
    NEED_USER = "need_user"
    RETRYABLE = "retryable"
    BLOCKED = "blocked"
    FAILED = "failed"


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
class AgentTask:
    """The objective handed to one skill agent — a bare task string."""

    objective: str

    def __post_init__(self) -> None:
        objective = self.objective.strip()
        if not objective:
            raise ValueError("objective must be non-empty")
        object.__setattr__(self, "objective", objective)

    def to_dict(self) -> dict[str, str]:
        return {"objective": self.objective}


@dataclass(frozen=True)
class DelegationOutcome:
    """Bounded result of a trusted skill-agent execution."""

    skill: str
    status: DelegationStatus | str
    summary: str | None = None
    refs: tuple[ArtifactRef, ...] | list[ArtifactRef | Mapping[str, Any]] = ()
    error_code: str | None = None

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

    def safe_refs(self) -> list[dict[str, str]]:
        return [ref.to_dict() for ref in self.refs]


def _coerce_artifact_ref(value: ArtifactRef | Mapping[str, Any]) -> ArtifactRef:
    if isinstance(value, ArtifactRef):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("refs entries must be objects")
    # ``quality`` is a documented projection field on EvidenceStore refs; the
    # controller feeds those projections straight into DelegationOutcome, so
    # it is tolerated here rather than treated as a foreign key.
    allowed = {"artifact_id", "source_url", "content_hash", "artifact_type", "quality"}
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


__all__ = [
    "AgentTask",
    "ArtifactRef",
    "DelegationOutcome",
    "DelegationStatus",
]
