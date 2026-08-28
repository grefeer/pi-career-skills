"""Adapt pi-career-skills ``ToolDefinition``s to langchain ``BaseTool``.

Mirrors ``pi_career_skills.tool_adapter``: each registered definition becomes
a ``BaseTool`` whose ``_run`` executes the same trusted-kernel path
(``invoke_tool_sync``) and returns the same model-visible content — bounded
output JSON on success, a short error envelope on failure (no raw exceptions
ever reach the model).

The tool observes its ``tool_call_id`` through langchain's
``InjectedToolCallId`` mechanism and records the produced ``ToolObservation``
into the run's ``HarnessState`` pending channel; ``EvidenceMiddleware`` pops
it after the handler and drives evidence promotion, the tool-call guard,
stall steering and event logging (pi hook semantics — see MIGRATION.md §3).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Annotated, Any

from langchain_core.tools import BaseTool, InjectedToolCallId
from pydantic import BaseModel, create_model

from pi_career_skills.agents.capabilities import CAPABILITY_REGISTRY
from pi_career_skills.context import ToolContext
from pi_career_skills.contracts import ToolObservation
from pi_career_skills.registry import (
    CareerToolRegistry,
    ToolDefinition,
)
from pi_career_skills.tool_adapter import bound_content, invoke_tool_sync

from .run_state import HarnessState


def _args_schema_with_tool_call_id(
    input_model: type[BaseModel],
) -> type[BaseModel]:
    """Input model plus a hidden ``tool_call_id`` filled in by langchain.

    ``InjectedToolCallId`` keeps the field out of the model-visible JSON
    schema while letting langgraph thread the call id into ``_run``.
    """
    return create_model(
        input_model.__name__,
        __base__=input_model,
        tool_call_id=(Annotated[str, InjectedToolCallId], ""),
    )


def _content_from_observation(obs: ToolObservation) -> str:
    """Project an observation into the model-visible tool content."""
    if obs.status == "succeeded":
        payload = bound_content(obs.output or {})
        return json.dumps(payload, ensure_ascii=False)
    return json.dumps(
        {
            "status": obs.status,
            "error_code": obs.error_code,
            "error_message": obs.error_message,
        },
        ensure_ascii=False,
    )


class CareerLangchainTool(BaseTool):
    """One registered career tool as a langchain ``BaseTool``.

    Model-visible contract is ``definition.agent_description``; input
    validation/output bounding are delegated to the trusted kernel
    (``invoke_tool_sync``), so behavior matches the pi adapter exactly.
    """

    _registry: CareerToolRegistry
    _context: ToolContext
    _state: HarnessState | None
    _artifact_type: str | None

    def __init__(
        self,
        definition: ToolDefinition,
        registry: CareerToolRegistry,
        context: ToolContext,
        state: HarnessState | None = None,
    ) -> None:
        super().__init__(
            name=definition.name,
            description=definition.agent_description,
            args_schema=_args_schema_with_tool_call_id(definition.input_model),
        )
        self._registry = registry
        self._context = context
        self._state = state
        self._artifact_type = definition.artifact_type

    def _run(self, **kwargs: Any) -> str:
        tool_call_id: str | None = kwargs.pop("tool_call_id", None) or None
        context = self._context
        if self._state is not None:
            context = replace(
                context,
                metadata=self._state.projected_metadata(context.skill_name or ""),
            )
        obs = invoke_tool_sync(
            self._registry,
            context,
            self.name,
            tool_call_id,
            kwargs,
            enforce_skill_isolation=True,
        )
        if self._state is not None and tool_call_id:
            self._state.record_observation(tool_call_id, obs)
        return _content_from_observation(obs)

    async def _arun(self, **kwargs: Any) -> str:
        # Handlers are short deterministic functions; offload like pi's
        # ``asyncio.to_thread`` so a slow handler never blocks the loop.
        return await asyncio.to_thread(self._run, **kwargs)


def build_skill_tools(
    skill_name: str,
    registry: CareerToolRegistry,
    context: ToolContext,
    state: HarnessState | None = None,
) -> list[CareerLangchainTool]:
    """Build the per-skill tool grant (catalog order, definitions resolved).

    Unknown names in the catalog are skipped defensively.
    """
    tools: list[CareerLangchainTool] = []
    names = CAPABILITY_REGISTRY.require(skill_name).tool_names
    for name in names:
        definition = registry.get(name)
        if definition is not None:
            tools.append(
                CareerLangchainTool(definition, registry, context, state=state)
            )
    return tools


__all__ = [
    "CareerLangchainTool",
    "build_skill_tools",
]
