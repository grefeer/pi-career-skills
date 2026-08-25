"""Supervisor + per-delegation skill agents (migration plan §4.2).

The supervisor sees ONLY the four ``delegate-<skill>`` tools; the business
tools are never granted to it (tool_adapter re-checks skill isolation as
defense in depth anyway).  Each delegation runs a FRESH skill agent with
exactly its own skill's catalog (10 / 1 / 1 / 1) and its curated prompt —
messages are never shared or reused across delegations.

Budgets, evidence promotion and events are NOT wired here; the Phase 7
controller owns them (including the shared EvidenceStore).
"""

from __future__ import annotations

from typing import Any

from pi_agent_core import Agent, AgentOptions, AgentState
from pi_ai import Model

from ..context import ToolContext
from ..registry import CareerToolRegistry, build_career_tool_registry
from ..tool_adapter import make_agent_tool
from .capabilities import CAPABILITY_REGISTRY
from .delegation_tools import DelegationRunner, make_delegation_tool
from .prompts import (
    CAREER_PLANNING_PROMPT,
    JOB_DISCOVERY_PROMPT,
    JOB_MATCHING_PROMPT,
    RESUME_TAILORING_PROMPT,
    SUPERVISOR_PROMPT,
    load_archived_skill_prompt,
)

#: Curated prompt per skill (single source: agents/prompts.py).
_SKILL_PROMPTS: dict[str, str] = {
    "job-discovery": JOB_DISCOVERY_PROMPT,
    "job-matching": JOB_MATCHING_PROMPT,
    "resume-tailoring": RESUME_TAILORING_PROMPT,
    "career-planning": CAREER_PLANNING_PROMPT,
}

#: The supervisor may delegate to exactly these skills, in this order.
_SUPERVISOR_SKILLS: tuple[str, ...] = tuple(CAPABILITY_REGISTRY)

#: Registered definitions — resolves catalog names to ``ToolDefinition``.
_REGISTRY = build_career_tool_registry()


def build_supervisor_agent(
    model: Model,
    runner: DelegationRunner | dict[str, DelegationRunner],
    *,
    allowed_skills: tuple[str, ...] | None = None,
    registry: Any = None,
    stream_fn: Any = None,
    get_api_key: Any = None,
    before_tool_call: Any = None,
    after_tool_call: Any = None,
    should_stop_after_turn: Any = None,
) -> Agent:
    """Build the supervisor agent with delegation tools for allowed skills.

    When ``allowed_skills`` is ``None`` (default), all four skills are
    exposed — identical to the historical behavior.  Otherwise only the
    skills present in *both* ``allowed_skills`` and ``_SUPERVISOR_SKILLS``
    are included, preserving ``_SUPERVISOR_SKILLS`` order.

    ``runner`` can be either a single ``DelegationRunner`` used for every
    skill (backward-compatible) or a ``{skill: runner}`` mapping for
    per-skill runners (useful when each delegation needs its own closure).

    All hook kwargs are optional and default to ``None`` (same as before).
    ``registry`` is unused for the supervisor (it only sees delegation tools);
    the parameter is accepted for call-site symmetry with ``build_skill_agent``.
    """
    if allowed_skills is None:
        skills = list(_SUPERVISOR_SKILLS)
    else:
        allowed_set = set(allowed_skills)
        skills = [s for s in _SUPERVISOR_SKILLS if s in allowed_set]

    if isinstance(runner, dict):
        tools = [make_delegation_tool(s, runner[s]) for s in skills]
    else:
        tools = [make_delegation_tool(s, runner) for s in skills]

    return Agent(
        AgentOptions(
            initial_state=AgentState(
                system_prompt=SUPERVISOR_PROMPT,
                model=model,
                tools=tools,
            ),
            tool_execution="sequential",
            stream_fn=stream_fn,
            get_api_key=get_api_key,
            before_tool_call=before_tool_call,
            after_tool_call=after_tool_call,
            should_stop_after_turn=should_stop_after_turn,
        )
    )


def build_skill_agent(
    skill_name: str,
    model: Model,
    context: ToolContext,
    *,
    registry: Any = None,
    stream_fn: Any = None,
    get_api_key: Any = None,
    before_tool_call: Any = None,
    after_tool_call: Any = None,
    should_stop_after_turn: Any = None,
) -> Agent:
    """Build a fresh skill agent scoped to *skill_name*'s own tools only.

    When ``registry`` is given, catalog names are resolved from it instead of
    the module-level ``_REGISTRY`` (useful for hermetic tests with stub handlers).

    Raises:
        ValueError: for an unknown ``skill_name``.
    """
    if skill_name not in CAPABILITY_REGISTRY:
        raise ValueError(f"unknown skill: {skill_name}")
    reg = registry if registry is not None else _REGISTRY
    # Hermetic test registries and downstream integrations may intentionally
    # provide an older catalog.  Keep the capability contract authoritative,
    # but omit definitions unavailable in that injected registry; the default
    # production registry contains every current tool, including the shared
    # read-skill-reference tool.
    missing = [name for name in CAPABILITY_REGISTRY[skill_name].tool_names if reg.get(name) is None]
    if missing and isinstance(reg, CareerToolRegistry):
        raise RuntimeError(
            f"production registry is missing tools for {skill_name}: {', '.join(missing)}"
        )
    tools = [
        make_agent_tool(definition, context)
        for name in CAPABILITY_REGISTRY[skill_name].tool_names
        if (definition := reg.get(name)) is not None
    ]
    return Agent(
        AgentOptions(
            initial_state=AgentState(
                system_prompt=load_archived_skill_prompt(
                    skill_name, _SKILL_PROMPTS[skill_name]
                ),
                model=model,
                tools=tools,
            ),
            tool_execution="sequential",
            stream_fn=stream_fn,
            get_api_key=get_api_key,
            before_tool_call=before_tool_call,
            after_tool_call=after_tool_call,
            should_stop_after_turn=should_stop_after_turn,
        )
    )


__all__ = [
    "build_supervisor_agent",
    "build_skill_agent",
]
