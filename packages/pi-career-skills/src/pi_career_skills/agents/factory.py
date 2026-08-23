"""Supervisor + per-delegation skill agents (migration plan §4.2).

The supervisor sees ONLY the three ``delegate-<skill>`` tools; the business
tools are never granted to it (tool_adapter re-checks skill isolation as
defense in depth anyway).  Each delegation runs a FRESH skill agent with
exactly its own skill's catalog (10 / 1 / 1) and its curated prompt —
messages are never shared or reused across delegations.

Budgets, evidence promotion and events are NOT wired here; the Phase 7
controller owns them (including the shared EvidenceStore).
"""

from __future__ import annotations

from pi_agent_core import Agent, AgentOptions, AgentState
from pi_ai import Model

from ..context import ToolContext
from ..registry import TOOL_CATALOG_BY_SKILL, build_career_tool_registry
from ..tool_adapter import make_agent_tool
from .delegation_tools import DelegationRunner, make_delegation_tool
from .prompts import (
    JOB_DISCOVERY_PROMPT,
    JOB_MATCHING_PROMPT,
    RESUME_TAILORING_PROMPT,
    SUPERVISOR_PROMPT,
)

#: Curated prompt per skill (single source: agents/prompts.py).
_SKILL_PROMPTS: dict[str, str] = {
    "job-discovery": JOB_DISCOVERY_PROMPT,
    "job-matching": JOB_MATCHING_PROMPT,
    "resume-tailoring": RESUME_TAILORING_PROMPT,
}

#: The supervisor may delegate to exactly these skills, in this order.
_SUPERVISOR_SKILLS: tuple[str, ...] = ("job-discovery", "job-matching", "resume-tailoring")

#: Registered definitions — resolves catalog names to ``ToolDefinition``.
_REGISTRY = build_career_tool_registry()


def build_supervisor_agent(model: Model, runner: DelegationRunner) -> Agent:
    """Build the supervisor agent with exactly the three delegation tools."""
    return Agent(
        AgentOptions(
            initial_state=AgentState(
                system_prompt=SUPERVISOR_PROMPT,
                model=model,
                tools=[make_delegation_tool(s, runner) for s in _SUPERVISOR_SKILLS],
            ),
            tool_execution="sequential",
        )
    )


def build_skill_agent(skill_name: str, model: Model, context: ToolContext) -> Agent:
    """Build a fresh skill agent scoped to *skill_name*'s own tools only.

    Raises:
        ValueError: for an unknown ``skill_name``.
    """
    if skill_name not in TOOL_CATALOG_BY_SKILL:
        raise ValueError(f"unknown skill: {skill_name}")
    tools = [
        make_agent_tool(_REGISTRY[name], context)
        for name in TOOL_CATALOG_BY_SKILL[skill_name]
    ]
    return Agent(
        AgentOptions(
            initial_state=AgentState(
                system_prompt=_SKILL_PROMPTS[skill_name],
                model=model,
                tools=tools,
            ),
            tool_execution="sequential",
        )
    )


__all__ = [
    "build_supervisor_agent",
    "build_skill_agent",
]
