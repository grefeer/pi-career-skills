"""Restricted, on-demand reads from archived skill reference documents."""

from __future__ import annotations

from importlib import resources
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..context import ToolContext
from ..errors import (
    SKILL_REFERENCE_NOT_ALLOWED,
    SKILL_REFERENCE_NOT_FOUND,
    CareerToolError,
)

READ_REFERENCE_SKILLS = frozenset(
    {"job-discovery", "job-matching", "resume-tailoring", "career-planning"}
)

# Deliberately static: callers cannot use this tool to enumerate package files.
SKILL_REFERENCE_ALLOWLIST: dict[str, frozenset[str]] = {
    "career-planning": frozenset({"references/schema.md"}),
    "job-discovery": frozenset(
        {
            "references/anti-crawl-guide.md",
            "references/browse-modes.md",
            "references/extraction-guide.md",
            "references/incremental-persistence.md",
            "references/schema.md",
            "references/single-url-extraction.md",
            "references/site-adapters.md",
            "references/site-catalog.md",
            "references/smartsheet-sources.md",
            "references/wechat-image-handling.md",
        }
    ),
    "job-matching": frozenset({"references/schema.md"}),
    "resume-tailoring": frozenset(
        {"references/schema.md", "references/tailoring-guide.md"}
    ),
}


class ReadSkillReferenceInput(BaseModel):
    """A relative, allowlisted reference path and a bounded read size."""

    reference: str = Field(min_length=1, max_length=128)
    max_chars: int = Field(default=8_000, ge=256, le=12_000)

    @field_validator("reference")
    @classmethod
    def validate_reference_path(cls, value: str) -> str:
        if (
            "\\" in value
            or "\x00" in value
            or value.startswith(("/", "~"))
            or ".." in value.split("/")
            or not value.startswith("references/")
            or not value.endswith(".md")
        ):
            raise ValueError("reference must be a relative references/*.md path")
        return value


class ReadSkillReferenceOutput(BaseModel):
    skill_name: str
    reference: str
    content: str
    truncated: bool
    source: str


def read_skill_reference(
    context: ToolContext, params: ReadSkillReferenceInput
) -> dict[str, Any]:
    """Read one allowlisted reference belonging to the current skill agent."""

    skill_name = context.skill_name
    if skill_name not in READ_REFERENCE_SKILLS:
        raise CareerToolError(
            SKILL_REFERENCE_NOT_ALLOWED,
            "read-skill-reference requires a scoped skill agent",
        )
    allowed = SKILL_REFERENCE_ALLOWLIST[skill_name]
    if params.reference not in allowed:
        raise CareerToolError(
            SKILL_REFERENCE_NOT_ALLOWED,
            f"reference is not allowlisted for skill {skill_name}",
        )

    resource = resources.files("pi_career_skills.resources.archived_skills").joinpath(
        skill_name, *params.reference.split("/")
    )
    try:
        text = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError) as exc:
        raise CareerToolError(
            SKILL_REFERENCE_NOT_FOUND, "allowlisted skill reference is unavailable"
        ) from exc

    return {
        "skill_name": skill_name,
        "reference": params.reference,
        "content": text[: params.max_chars],
        "truncated": len(text) > params.max_chars,
        "source": f"archived_skills/{skill_name}/{params.reference}",
    }


__all__ = [
    "READ_REFERENCE_SKILLS",
    "SKILL_REFERENCE_ALLOWLIST",
    "ReadSkillReferenceInput",
    "ReadSkillReferenceOutput",
    "read_skill_reference",
]
