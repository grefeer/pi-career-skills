"""Per-skill agents for the deterministic skill pipeline (migration §4.2).

Each pipeline node runs a FRESH skill agent built on the shared
``pi_coding_agent.CodingAgent`` SDK wrapper (over ``pi_agent_core.Agent``),
always with ``tool_execution="sequential"`` and the controller hooks wired
through.  A skill agent sees exactly its own skill's catalog (from
``CAPABILITY_REGISTRY``) and its curated prompt — messages are never shared
or reused across nodes.

Budgets, evidence promotion and events are NOT wired here; the Phase 7
controller owns them (including the shared EvidenceStore).
"""

from __future__ import annotations

from typing import Any

from pi_ai import Model
from pi_coding_agent import CodingAgent

from ..context import ToolContext
from ..registry import CAREER_TOOL_REGISTRY, CareerToolRegistry
from ..tool_adapter import make_agent_tool
from .capabilities import CAPABILITY_REGISTRY
from .prompts import (
    CAREER_PLANNING_PROMPT,
    JOB_DISCOVERY_PROMPT,
    JOB_MATCHING_PROMPT,
    RESUME_TAILORING_PROMPT,
    load_archived_skill_prompt,
)

#: Curated prompt per skill (single source: agents/prompts.py).
_SKILL_PROMPTS: dict[str, str] = {
    "job-discovery": JOB_DISCOVERY_PROMPT,
    "job-matching": JOB_MATCHING_PROMPT,
    "resume-tailoring": RESUME_TAILORING_PROMPT,
    "career-planning": CAREER_PLANNING_PROMPT,
}

#: Registered definitions — resolves catalog names to ``ToolDefinition``.
_REGISTRY = CAREER_TOOL_REGISTRY


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
) -> CodingAgent:
    """Build a fresh skill agent scoped to *skill_name*'s own tools only.

    The agent is a ``pi_coding_agent.CodingAgent`` (the shared SDK wrapper
    over ``pi_agent_core.Agent``) with ``tool_execution="sequential"`` and
    the controller hooks wired through.

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
    return CodingAgent(
        model=model,
        system_prompt=load_archived_skill_prompt(
            skill_name, _SKILL_PROMPTS[skill_name]
        ),
        tools=tools,
        tool_execution="sequential",
        stream_fn=stream_fn,
        get_api_key=get_api_key,
        before_tool_call=before_tool_call,
        after_tool_call=after_tool_call,
        should_stop_after_turn=should_stop_after_turn,
    )


__all__ = [
    "build_skill_agent",
]
