from __future__ import annotations

from pi_career_skills.business.skill_references import (
    ReadSkillReferenceInput,
    read_skill_reference,
)
from pi_career_skills.context import ToolContext
from pi_career_skills.errors import CareerToolError
from pi_career_skills.registry import build_career_tool_registry
from pi_career_skills.tool_adapter import make_agent_tool


def _context(skill: str | None) -> ToolContext:
    return ToolContext(user_id="u", run_id="r", skill_name=skill)


def test_reads_reference_for_current_skill() -> None:
    result = read_skill_reference(
        _context("job-discovery"),
        ReadSkillReferenceInput(reference="references/schema.md", max_chars=300),
    )
    assert result["skill_name"] == "job-discovery"
    assert result["source"] == (
        "archived_skills/job-discovery/references/schema.md"
    )
    assert result["content"]
    assert len(result["content"]) <= 300


def test_rejects_cross_skill_or_non_reference_paths() -> None:
    try:
        read_skill_reference(
            _context("job-matching"),
            ReadSkillReferenceInput(reference="references/tailoring-guide.md"),
        )
    except CareerToolError as exc:
        assert exc.code == "skill_reference_not_allowed"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("cross-skill reference unexpectedly read")


def test_rejects_unscoped_agent_and_path_traversal() -> None:
    try:
        read_skill_reference(
            _context(None), ReadSkillReferenceInput(reference="references/schema.md")
        )
    except CareerToolError as exc:
        assert exc.code == "skill_reference_not_allowed"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("unscoped agent unexpectedly read a reference")

    for path in ("SKILL.md", "references/../SKILL.md", "../references/schema.md"):
        try:
            ReadSkillReferenceInput(reference=path)
        except ValueError:
            pass
        else:  # pragma: no cover - assertion guard
            raise AssertionError(f"unsafe path accepted: {path}")


def test_truncates_large_reference() -> None:
    result = read_skill_reference(
        _context("job-discovery"),
        ReadSkillReferenceInput(reference="references/anti-crawl-guide.md", max_chars=256),
    )
    assert result["truncated"] is True
    assert len(result["content"]) == 256


async def test_model_facing_tool_enforces_current_skill_scope() -> None:
    registry = build_career_tool_registry()
    definition = registry["read-skill-reference"]

    allowed = await make_agent_tool(definition, _context("job-discovery")).execute(
        "read-1", {"reference": "references/schema.md"}
    )
    assert allowed.details.status == "succeeded"

    forbidden = await make_agent_tool(definition, _context(None)).execute(
        "read-2", {"reference": "references/schema.md"}
    )
    assert forbidden.details.status == "failed"
    assert forbidden.details.error_code == "tool_skill_forbidden"
