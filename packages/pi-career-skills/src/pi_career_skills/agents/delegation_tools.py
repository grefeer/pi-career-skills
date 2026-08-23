"""Supervisor delegation tools — ``delegate-<skill>`` (migration plan §6.7).

The supervisor owns exactly three tools, one per career skill.  Each tool
hands the task goal to a ``DelegationRunner`` supplied by the run-level
controller (Phase 7 wires budgets, evidence promotion and the skill agent
loop there; tests supply a fake).  The model-visible result carries only a
bounded summary + evidence refs — never the full private context.

§6.7 semantics preserved here:
- success returns ``terminate=True`` so the supervisor stops and finalizes;
- controlled errors return ``terminate=False`` (the supervisor prompt tells
  the model to pick a different allowed skill or produce its final answer)
  and the model-visible message always contains the literal error code —
  no text-level fake success.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pi_agent_core.types import AgentTool, AgentToolResult, ToolExecutionMode
from pi_ai import TextContent

from ..errors import (
    DELEGATION_SKILL_ALREADY_SUCCEEDED,
    TARGET_EVIDENCE_NOT_FOUND,
    CareerToolError,
)
from ..runtime.completion import _bounded_summary


@dataclass
class DelegationOutcome:
    """One delegation verdict produced by the controller's runner."""

    skill: str
    status: Literal["succeeded", "error"]
    summary: str | None = None
    refs: list[dict[str, Any]] | None = None
    error_code: str | None = None


#: Runner contract: ``(task_goal, params) -> DelegationOutcome``.  The
#: controller (Phase 7) supplies the real runner; tests supply a fake.
DelegationRunner = Callable[[str, dict[str, Any]], DelegationOutcome]

#: Model-visible ``task_goal`` description per skill (short Chinese).
_GOAL_DESCRIPTIONS: dict[str, str] = {
    "job-discovery": "委托 job-discovery 技能收集公开职位页面证据并提取结构化 JD",
    "job-matching": "委托 job-matching 技能对本次运行已观察职位做透明可追溯的匹配排序",
    "resume-tailoring": "委托 resume-tailoring 技能针对目标 JD 生成简历修改建议",
}


def _error_message(skill: str, code: str | None) -> str:
    """Model-visible error text — always carries the literal error code."""
    if code == DELEGATION_SKILL_ALREADY_SUCCEEDED:
        return f"技能 {skill} 已完成委托，无需重复执行（{DELEGATION_SKILL_ALREADY_SUCCEEDED}）"
    if code == TARGET_EVIDENCE_NOT_FOUND:
        return f"缺少已观察的职位证据，无法执行 {skill}（{TARGET_EVIDENCE_NOT_FOUND}）"
    return f"委托 {skill} 失败（{code}）"


class _DelegationAgentTool:
    """One ``delegate-<skill>`` tool bound to a runner."""

    def __init__(self, skill: str, runner: DelegationRunner) -> None:
        if skill not in _GOAL_DESCRIPTIONS:
            raise ValueError(f"unknown delegation skill: {skill}")
        self._skill = skill
        self._runner = runner
        self.name = f"delegate-{skill}"
        self.description = (
            f"把任务委托给 {skill} 技能代理执行（只使用该技能的工具）；"
            "返回有限的交付摘要与证据引用，不返回完整私有上下文。"
        )
        self.parameters: dict[str, Any] = {
            "type": "object",
            "properties": {
                "task_goal": {
                    "type": "string",
                    "description": _GOAL_DESCRIPTIONS[skill],
                }
            },
            "required": ["task_goal"],
            "additionalProperties": False,
        }
        self.label = skill
        self.execution_mode: ToolExecutionMode | None = "sequential"

    def _error_result(self, code: str | None) -> AgentToolResult:
        return AgentToolResult(
            content=[TextContent(text=_error_message(self._skill, code))],
            details={
                "skill": self._skill,
                "status": "error",
                "error_code": code,
            },
            terminate=False,
        )

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        cancel_event: asyncio.Event | None = None,
        on_update: Any = None,
    ) -> AgentToolResult:
        """Run the delegation off the event loop and project the outcome.

        A runner raising ``CareerToolError`` is a controlled error — same
        message rule as a returned ``error`` outcome.
        """
        del tool_call_id, cancel_event, on_update
        task_goal = params.get("task_goal", "")
        try:
            outcome = await asyncio.to_thread(self._runner, task_goal, params)
        except CareerToolError as exc:
            return self._error_result(exc.code)
        if outcome.status == "succeeded":
            bounded = _bounded_summary(outcome.summary)
            if bounded is None:
                bounded = f"技能 {self._skill} 已完成。"
            return AgentToolResult(
                content=[TextContent(text=bounded)],
                details={
                    "skill": self._skill,
                    "status": "succeeded",
                    "refs": outcome.refs or [],
                },
                terminate=True,
            )
        return self._error_result(outcome.error_code)


def make_delegation_tool(skill: str, runner: DelegationRunner) -> AgentTool:
    """Build the ``delegate-<skill>`` tool for *runner*."""
    return _DelegationAgentTool(skill=skill, runner=runner)


__all__ = [
    "DelegationOutcome",
    "DelegationRunner",
    "make_delegation_tool",
]
