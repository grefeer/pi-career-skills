"""Snapshot gate for the tool registry (migration plan §3.1 / §6.4).

Compares the 15 registered tool definitions against
``fixtures/pi_contract_snapshot.json`` (generated from the source project):
name / skill_name / is_deliverable / artifact_type / description must match
exactly, and the input/output pydantic JSON schemas must be field-equal.
Descriptions are model-visible contract text — any drift fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pi_career_skills.registry import (
    TOOL_ARTIFACT_TYPE,
    TOOL_CATALOG_BY_SKILL,
    build_career_tool_registry,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_SNAPSHOT = json.loads((_FIXTURES / "pi_contract_snapshot.json").read_text("utf-8"))

EXPECTED_SKILL_COUNTS = {
    "job-discovery": 13,  # + browse-public-job-page, search-job-site
    "job-matching": 2,
    "resume-tailoring": 2,
    "career-planning": 2,
}
EXPECTED_DELIVERABLE_COUNTS = {
    # fetch-pages/page/wechat/search/sheets/extract/extract-batch/browse/search-site
    "job-discovery": 9,
    "job-matching": 1,
    "resume-tailoring": 1,
    "career-planning": 1,
}


@pytest.fixture(scope="module")
def registry() -> object:
    return build_career_tool_registry()


def _snapshot_tools() -> dict[str, dict]:
    return {tool["name"]: tool for tool in _SNAPSHOT["tools"]}


def test_snapshot_metadata() -> None:
    """Snapshot shape sanity: schema version + 15 tools."""
    assert _SNAPSHOT["schema_version"] == "pi_contract_v1"
    assert _SNAPSHOT["tool_count"] == 15
    assert len(_snapshot_tools()) == 15


def test_registry_has_exactly_the_snapshot_tool_names(registry: object) -> None:
    assert set(registry.tool_names()) == set(_snapshot_tools()) | {
        "read-skill-reference"
    }


def test_tool_catalog_by_skill_counts(registry: object) -> None:
    catalog = registry.catalog_by_skill()
    assert {skill: len(names) for skill, names in catalog.items()} == EXPECTED_SKILL_COUNTS
    assert {skill: len(names) for skill, names in TOOL_CATALOG_BY_SKILL.items()} == EXPECTED_SKILL_COUNTS


@pytest.mark.parametrize("tool_name", sorted(_snapshot_tools()))
def test_definition_matches_snapshot(registry: object, tool_name: str) -> None:
    """Every definition matches the snapshot field-for-field."""
    snapshot = _snapshot_tools()[tool_name]
    definition = registry.get(tool_name)
    assert definition is not None, f"{tool_name} missing from registry"

    assert definition.name == snapshot["name"]
    assert definition.skill_name == snapshot["skill_name"]
    # Source ToolDefinition defaults is_deliverable to None; pi uses False.
    # Both mean "not a deliverable" — compare as booleans.
    assert bool(definition.is_deliverable) == bool(snapshot["is_deliverable"])
    assert definition.artifact_type == snapshot["artifact_type"]
    assert definition.artifact_type == TOOL_ARTIFACT_TYPE.get(tool_name)
    assert definition.description == snapshot["description"]

    # JSON schemas must be field-equal (pydantic generates $defs/titles/etc).
    assert definition.input_model.model_json_schema() == snapshot["input_model"], (
        f"{tool_name} input_model drift"
    )
    assert definition.output_model.model_json_schema() == snapshot["output_model"], (
        f"{tool_name} output_model drift"
    )


def test_deliverable_counts_per_skill(registry: object) -> None:
    """Deliverable tools per skill match the plan §3.1 (9/1/1)."""
    counts: dict[str, int] = {}
    for name in registry.tool_names():
        definition = registry.get(name)
        if definition and definition.is_deliverable:
            counts[definition.skill_name] = counts.get(definition.skill_name, 0) + 1
    assert counts == EXPECTED_DELIVERABLE_COUNTS


def test_all_deliverables_have_artifact_type(registry: object) -> None:
    """is_deliverable implies a persisted artifact type (kernel boundary)."""
    for name in registry.tool_names():
        definition = registry.get(name)
        if definition and definition.is_deliverable:
            assert definition.artifact_type is not None, f"{name} missing artifact_type"
            assert name in TOOL_ARTIFACT_TYPE


def test_compound_tools_expose_atomicity_contract(registry: object) -> None:
    """Batch implementations remain available but advertise their boundary."""
    batch = registry.get("fetch-public-job-pages")
    extract_batch = registry.get("extract-observed-job-details-batch")
    single = registry.get("fetch-public-job-page")

    assert batch.contract.granularity == "batch"
    assert batch.contract.preferred_for_agent is False
    assert batch.contract.fallback_route == "fetch-public-job-page"
    assert extract_batch.contract.granularity == "batch"
    assert single.contract.granularity == "atomic"
    assert "granularity=atomic" in single.agent_description
