"""Adapt ``ToolDefinition``s to the pi-agent ``AgentTool`` protocol.

Two invoke entries share one execution core:

* ``invoke_tool`` (async) — the **model-facing** boundary.  Enforces skill
  isolation (a model may only call tools of the skill it is scoped to) and
  runs the sync handler off the event loop via ``asyncio.to_thread``.
* ``invoke_tool_sync`` / ``CareerToolRegistry.invoke`` (sync) — the
  **trusted-kernel** path (controller, deterministic matching fallback).
  No isolation, no thread offload: the kernel decides what it calls and
  handlers are pure CPU functions.

Both return ``ToolObservation`` — never raise.  Input and output are
validated against the definition's pydantic models; exceptions are converted
to failed/blocked observations with redacted messages (``errors.redact_message``).
The same run never executes business tools concurrently — the controller
serializes model tool calls, so the adapter needs no lock.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from pi_agent_core.types import AgentTool, AgentToolResult, ToolExecutionMode
from pi_ai import TextContent

from .context import ToolContext
from .contracts import ToolObservation
from .errors import (
    BLOCKED_ERROR_CODES,
    INVALID_TOOL_INPUT,
    INVALID_TOOL_OUTPUT,
    TOOL_EXECUTION_FAILED,
    TOOL_SKILL_FORBIDDEN,
    UNKNOWN_TOOL,
    CareerToolError,
    redact_message,
)
from .registry import ToolDefinition


def bound_content(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded copy of *value* (40 / 12_000 / 20 / 1_200).

    Mirrors ``backend/app/services/agent_kernel/evidence.py``
    ``_bounded_content`` exactly: at most 40 top-level fields, strings capped
    at 12 000 chars, lists capped at 20 items, nested strings at 1 200 chars.
    The harness and the model-facing content both use this bound so a runaway
    tool output can never blow up the context or the artifact table.
    """
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:40]:
        if isinstance(item, str):
            result[str(key)] = item[:12_000]
        elif isinstance(item, (int, float, bool)) or item is None:
            result[str(key)] = item
        elif isinstance(item, list):
            result[str(key)] = [
                (
                    bound_content(nested)
                    if isinstance(nested, Mapping)
                    else str(nested)[:1_200]
                )
                for nested in item[:20]
            ]
        elif isinstance(item, Mapping):
            result[str(key)] = bound_content(item)
    return result


# ---------------------------------------------------------------------------
# Execution core
# ---------------------------------------------------------------------------


def _is_skill_allowed(context: ToolContext, definition: ToolDefinition) -> bool:
    """True when *context* is scoped to exactly *definition*'s skill.

    ``skill_name=None`` (supervisor or non-skill context) is never allowed to
    call a business tool directly.
    """
    return (
        context.skill_name is not None
        and context.skill_name == definition.skill_name
    )


def _invalid_input_observation(
    definition: ToolDefinition, exc: Any, tool_call_id: str | None
) -> ToolObservation:
    return ToolObservation(
        tool_name=definition.name,
        status="failed",
        error_code=INVALID_TOOL_INPUT,
        error_message=redact_message(f"invalid tool input: {exc}"),
        tool_call_id=tool_call_id,
    )


def _invalid_output_observation(
    definition: ToolDefinition, exc: Any, tool_call_id: str | None
) -> ToolObservation:
    return ToolObservation(
        tool_name=definition.name,
        status="failed",
        error_code=INVALID_TOOL_OUTPUT,
        error_message=redact_message(f"invalid tool output: {exc}"),
        tool_call_id=tool_call_id,
    )


def _execute_definition(
    definition: ToolDefinition,
    context: ToolContext,
    params: dict[str, Any],
    tool_call_id: str | None,
) -> ToolObservation:
    """Validate input -> run handler -> validate output; never raises."""
    # 1. Input validation — the model never passes unvalidated params through.
    try:
        validated_input = definition.input_model.model_validate(params)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError et al.
        return _invalid_input_observation(definition, exc, tool_call_id)

    # 2. Handler execution.
    try:
        raw_result = definition.handler(context, validated_input)
    except CareerToolError as exc:
        status = "blocked" if exc.code in BLOCKED_ERROR_CODES else "failed"
        return ToolObservation(
            tool_name=definition.name,
            status=status,
            error_code=exc.code,
            error_message=redact_message(exc.message),
            tool_call_id=tool_call_id,
        )
    except Exception as exc:  # noqa: BLE001 - adapter boundary converts
        return ToolObservation(
            tool_name=definition.name,
            status="failed",
            error_code=TOOL_EXECUTION_FAILED,
            error_message=redact_message(str(exc)),
            tool_call_id=tool_call_id,
        )

    # 3. Output validation — only validated output becomes observation output.
    try:
        validated_output = definition.output_model.model_validate(raw_result)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError et al.
        return _invalid_output_observation(definition, exc, tool_call_id)

    return ToolObservation(
        tool_name=definition.name,
        status="succeeded",
        output=validated_output.model_dump(mode="json"),
        tool_call_id=tool_call_id,
    )


# ---------------------------------------------------------------------------
# Invoke entries
# ---------------------------------------------------------------------------


def invoke_tool_sync(
    registry: Any,
    context: ToolContext,
    tool_name: str,
    tool_call_id: str | None,
    params: dict[str, Any],
) -> ToolObservation:
    """Trusted-kernel sync invoke — unknown tools only; no skill isolation.

    See module docstring for why isolation does not apply here.  The kernel
    decides which tool it calls (e.g. the deterministic matching fallback),
    so a None-skill context is allowed.
    """
    definition = registry.get(tool_name)
    if definition is None:
        return ToolObservation(
            tool_name=tool_name,
            status="failed",
            error_code=UNKNOWN_TOOL,
            error_message=redact_message(f"unknown tool: {tool_name}"),
            tool_call_id=tool_call_id,
        )
    return _execute_definition(definition, context, params, tool_call_id)


async def invoke_tool(
    registry: Any,
    context: ToolContext,
    tool_name: str,
    tool_call_id: str | None,
    params: dict[str, Any],
) -> ToolObservation:
    """Model-facing async invoke with skill isolation + thread offload.

    The model may only call tools of the skill it is scoped to; a
    ``skill_name=None`` context (supervisor) is rejected with
    ``tool_skill_forbidden``.  Handlers run via ``asyncio.to_thread`` so a
    slow deterministic handler never blocks the event loop.
    """
    definition = registry.get(tool_name)
    if definition is None:
        return ToolObservation(
            tool_name=tool_name,
            status="failed",
            error_code=UNKNOWN_TOOL,
            error_message=redact_message(f"unknown tool: {tool_name}"),
            tool_call_id=tool_call_id,
        )
    if not _is_skill_allowed(context, definition):
        return ToolObservation(
            tool_name=tool_name,
            status="failed",
            error_code=TOOL_SKILL_FORBIDDEN,
            error_message=redact_message(
                f"tool {tool_name} requires skill {definition.skill_name}, "
                f"context skill: {context.skill_name or 'none'}"
            ),
            tool_call_id=tool_call_id,
        )
    return await asyncio.to_thread(
        _execute_definition, definition, context, params, tool_call_id
    )


# ---------------------------------------------------------------------------
# AgentTool factory
# ---------------------------------------------------------------------------


def _result_from_observation(observation: ToolObservation) -> AgentToolResult:
    """Project an observation into the model-visible AgentToolResult.

    ``content`` carries the bounded output JSON (success) or a short
    error envelope (failure); ``details`` carries the full observation for
    the harness and is never sent to the model.
    """
    if observation.status == "succeeded":
        payload = bound_content(observation.output or {})
        text = json.dumps(payload, ensure_ascii=False)
    else:
        text = json.dumps(
            {
                "status": observation.status,
                "error_code": observation.error_code,
                "error_message": observation.error_message,
            },
            ensure_ascii=False,
        )
    return AgentToolResult(
        content=[TextContent(text=text)],
        details=observation,
        terminate=False,
    )


class _RegisteredAgentTool:
    """Concrete ``AgentTool`` bound to one definition + run context."""

    def __init__(self, definition: ToolDefinition, context: ToolContext) -> None:
        self._definition = definition
        self._context = context
        self.name = definition.name
        self.description = definition.description
        self.parameters = definition.input_model.model_json_schema()
        self.label = definition.name
        self.execution_mode: ToolExecutionMode | None = "sequential"

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        cancel_event: asyncio.Event | None = None,
        on_update: Any = None,
    ) -> AgentToolResult:
        """Run the bound tool; cancel/update hooks are accepted, not used.

        Handlers are short deterministic functions; the run-level harness
        (not the tool) owns cancellation, so ``cancel_event`` is ignored.
        """
        del cancel_event, on_update
        definition = self._definition
        if not _is_skill_allowed(self._context, definition):
            observation = ToolObservation(
                tool_name=definition.name,
                status="failed",
                error_code=TOOL_SKILL_FORBIDDEN,
                error_message=redact_message(
                    f"tool {definition.name} requires skill "
                    f"{definition.skill_name}, context skill: "
                    f"{self._context.skill_name or 'none'}"
                ),
                tool_call_id=tool_call_id,
            )
        else:
            observation = await asyncio.to_thread(
                _execute_definition, definition, self._context, params, tool_call_id
            )
        return _result_from_observation(observation)


def make_agent_tool(definition: ToolDefinition, context: ToolContext) -> AgentTool:
    """Wrap one registered definition as a pi-agent ``AgentTool``.

    The returned tool runs ``sequential`` (never parallel) and re-checks
    skill isolation at execution time as defense in depth — the subagent
    grant should already match, but the adapter never trusts it alone.
    """
    return _RegisteredAgentTool(definition=definition, context=context)


__all__ = [
    "bound_content",
    "invoke_tool",
    "invoke_tool_sync",
    "make_agent_tool",
]
