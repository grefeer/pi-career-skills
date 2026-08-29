"""Phase 5 gate tests — skill-agent scoping + prompt provenance.

The LLM-routed supervisor and its ``delegate-<skill>`` tools are gone: the
pipeline is a deterministic loop over the fixed-order skill agents.  These
tests pin what remains of the old Phase 5 gate —
each skill agent sees exactly its own catalog plus the shared reference tool,
and the curated prompts are unchanged from the migration contract.
"""

from __future__ import annotations

import hashlib

import pytest

from pi_ai.providers.faux import FAUX_MODEL, clear_scripts
from pi_career_skills.agents.factory import build_skill_agent
from pi_career_skills.agents.prompts import (
    CAREER_PLANNING_PROMPT,
    JOB_DISCOVERY_PROMPT,
    JOB_MATCHING_PROMPT,
    PROMPT_HASHES,
    RESUME_TAILORING_PROMPT,
)
from pi_career_skills.context import ToolContext
from pi_career_skills.registry import TOOL_CATALOG_BY_SKILL

_CTX = ToolContext(user_id="user-1", run_id="run-1")


@pytest.fixture(autouse=True)
def _clear_faux_scripts() -> None:
    clear_scripts()
    yield
    clear_scripts()


def test_skill_scoping() -> None:
    """Each skill agent sees exactly its own catalog (13/2/2/2), nothing else."""
    counts = {skill: len(names) for skill, names in TOOL_CATALOG_BY_SKILL.items()}
    assert counts == {
        "job-discovery": 13,  # + browse-public-job-page, search-job-site
        "job-matching": 2,
        "resume-tailoring": 2,
        "career-planning": 2,
    }
    for skill, expected in TOOL_CATALOG_BY_SKILL.items():
        agent = build_skill_agent(skill, FAUX_MODEL, _CTX)
        names = [tool.name for tool in agent.state.tools]
        assert names == expected
        other_skill_names = {
            name
            for other, other_names in TOOL_CATALOG_BY_SKILL.items()
            if other != skill
            for name in other_names
        }
        assert (set(names) - {"read-skill-reference"}).isdisjoint(
            other_skill_names - {"read-skill-reference"}
        )


def test_each_skill_can_read_only_its_allowlisted_references() -> None:
    """The shared tool is exposed per agent but the handler enforces scope."""
    registry = __import__(
        "pi_career_skills.registry", fromlist=["build_career_tool_registry"]
    ).build_career_tool_registry()
    tool = registry["read-skill-reference"]
    assert tool.allowed_skills == frozenset(TOOL_CATALOG_BY_SKILL)


def test_unknown_skill_raises() -> None:
    with pytest.raises(ValueError):
        build_skill_agent("nope", FAUX_MODEL, _CTX)


#: sha256 (UTF-8) of the curated migration prompts, pinned at port time.
_VERBATIM_HASHES = {
    # Updated when P1/P2 documented browse-public-job-page / search-job-site
    # and the JS-rendering rule in JOB_DISCOVERY_PROMPT.
    "job-discovery": "4f457d3ece7c7aba71cc0fa6ccac644042f4ba4cd66f98ed98f2d0cac65165f8",
    "job-matching": "b1ec5fe666720c42d61a0d55d09294d799c929cea5d27781e34dd77f2cc80183",
    "resume-tailoring": "36bd80a0b851dcb6cde7c6612d3266bb4e846ed8e519a1de489bd11557d471e5",
    "career-planning": "5623795b2419c5972dd4726028f935a4c8cd71a3abbcd1e2c58d87961481a659",
}


def test_prompt_hashes() -> None:
    """PROMPT_HASHES covers exactly the four skill prompts, computed from the
    actual module strings, and matches the pinned migration contract."""
    prompts = {
        "job-discovery": JOB_DISCOVERY_PROMPT,
        "job-matching": JOB_MATCHING_PROMPT,
        "resume-tailoring": RESUME_TAILORING_PROMPT,
        "career-planning": CAREER_PLANNING_PROMPT,
    }
    assert set(PROMPT_HASHES) == set(prompts)
    for key, prompt in prompts.items():
        assert PROMPT_HASHES[key] == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    # Pinned at port time from the source anchors — a rewrite of any prompt
    # text breaks this test, not just the runtime map.
    assert PROMPT_HASHES == _VERBATIM_HASHES
