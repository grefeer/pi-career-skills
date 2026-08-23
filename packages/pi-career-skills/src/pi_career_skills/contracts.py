"""Typed value objects shared across the skill adapter boundary.

These are deterministic, model-agnostic contracts used by the registry,
tool adapter, and harness. They deliberately contain no runtime state
or LLM-facing logic.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolObservation(BaseModel):
    """Deterministic result envelope produced by every tool invocation.

    The harness, not the model, is the authority on status and error codes.
    ``output`` carries only validated, bounded dictionaries; raw exceptions
    never reach this object.
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    status: Literal["succeeded", "failed", "blocked"]
    output: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    tool_call_id: str | None = None


class Artifact(BaseModel):
    """One bounded, evidence-backed artifact produced by a deliverable tool.

    Artifacts are always content-addressed (``content_hash``) and traceable
    to a public source URL when applicable. The ``content`` dict is bounded
    by the adapter before it enters the harness.
    """

    artifact_id: str
    artifact_type: str
    tool_name: str
    source_url: str | None = None
    content_hash: str | None = None
    quality: str | None = None
    content: dict[str, Any] = Field(default_factory=dict)


class RunEvent(BaseModel):
    """One ordered event record emitted by the run lifecycle.

    ``payload`` is bounded and redacted by the caller; raw exceptions and
    secret-bearing strings never appear here.
    """

    seq: int
    type: str
    run_id: str
    attempt_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "ToolObservation",
    "Artifact",
    "RunEvent",
]
