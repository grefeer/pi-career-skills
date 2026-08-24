"""Supervisor delegation tools — ``delegate-<skill>`` (migration plan §6.7).

The supervisor owns exactly four tools, one per career skill.  Each tool
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
from typing import Any

from pi_agent_core.types import AgentTool, AgentToolResult, ToolExecutionMode
from pi_ai import TextContent

from ..errors import (
    DELEGATION_SKILL_ALREADY_SUCCEEDED,
    TARGET_EVIDENCE_NOT_FOUND,
    CareerToolError,
)
from ..runtime.completion import _bounded_summary
from .contracts import (
    AgentTask,
    DelegationOutcome,
    DelegationStatus,
    normalize_agent_task,
)

# New runners receive ``AgentTask``.  The legacy string signature remains
# accepted by the adapter for existing evaluation fakes and callers.
DelegationRunner = Callable[[AgentTask, dict[str, Any]], DelegationOutcome] | Callable[
    [str, dict[str, Any]], DelegationOutcome
]

#: Model-visible task description per skill (short Chinese).
_GOAL_DESCRIPTIONS: dict[str, str] = {
    "job-discovery": "委托 job-discovery 技能收集公开职位页面证据并提取结构化 JD",
    "job-matching": "委托 job-matching 技能对本次运行已观察职位做透明可追溯的匹配排序",
    "resume-tailoring": "委托 resume-tailoring 技能针对目标 JD 生成简历修改建议",
    "career-planning": "委托 career-planning 技能基于目标 JD 生成求职准备计划",
}


def _error_message(skill: str, code: str | None) -> str:
    """Model-visible error text — always carries the literal error code."""
    if code == DELEGATION_SKILL_ALREADY_SUCCEEDED:
        return f"技能 {skill} 已完成委托，无需重复执行（{DELEGATION_SKILL_ALREADY_SUCCEEDED}）"
    if code == TARGET_EVIDENCE_NOT_FOUND:
        return f"缺少已观察的职位证据，无法执行 {skill}（{TARGET_EVIDENCE_NOT_FOUND}）"
    return f"委托 {skill} 失败（{code}）"


def _status_message(outcome: DelegationOutcome) -> str:
    """Keep status/action visible without exposing private evidence content."""
    if outcome.status is DelegationStatus.SUCCESS:
        return _bounded_summary(outcome.summary) or f"技能 {outcome.skill} 已完成。"
    code = outcome.error_code or "unknown"
    return (
        f"技能 {outcome.skill} 返回 {outcome.status.value}，"
        f"建议动作 {outcome.action.value}（{code}）"
    )


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
                "objective": {
                    "type": "string",
                    "description": _GOAL_DESCRIPTIONS[skill],
                },
                "task_goal": {
                    "type": "string",
                    "description": "兼容旧调用；新调用请使用 objective。",
                },
                "input_refs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "artifact_id": {"type": "string"},
                            "source_url": {"type": "string"},
                            "content_hash": {"type": "string"},
                            "artifact_type": {"type": "string"},
                        },
                        "required": ["artifact_id"],
                        "additionalProperties": False,
                    },
                },
                "constraints": {"type": "object"},
                "expected_output": {
                    "oneOf": [
                        {"type": "string"},
                        {
                            "type": "object",
                            "properties": {
                                "artifact_type": {"type": "string"},
                                "requires_deliverable": {"type": "boolean"},
                            },
                            "required": ["artifact_type"],
                            "additionalProperties": False,
                        },
                    ]
                },
            },
            "anyOf": [{"required": ["objective"]}, {"required": ["task_goal"]}],
            "additionalProperties": False,
        }
        self.label = skill
        self.execution_mode: ToolExecutionMode | None = "sequential"

    def _error_result(
        self,
        outcome: DelegationOutcome,
        *,
        legacy: bool,
    ) -> AgentToolResult:
        status = "error" if legacy else outcome.status.value
        return AgentToolResult(
            content=[
                TextContent(
                    text=_error_message(self._skill, outcome.error_code)
                    if legacy
                    else _status_message(outcome)
                )
            ],
            details={
                "skill": self._skill,
                "status": status,
                **({} if legacy else {"action": outcome.action.value}),
                "error_code": outcome.error_code,
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
        task = normalize_agent_task(params)
        legacy = "objective" not in params
        try:
            runner_input: AgentTask | str = task.objective if legacy else task
            outcome = await asyncio.to_thread(self._runner, runner_input, params)
        except CareerToolError as exc:
            outcome = DelegationOutcome(
                skill=self._skill,
                status=DelegationStatus.FAILED,
                error_code=exc.code,
            )
            return self._error_result(outcome, legacy=legacy)
        if outcome.status is DelegationStatus.SUCCESS:
            return AgentToolResult(
                content=[TextContent(text=_status_message(outcome))],
                details={
                    "skill": self._skill,
                    "status": "succeeded" if legacy else outcome.status.value,
                    **({} if legacy else {"action": outcome.action.value}),
                    "refs": outcome.safe_refs(),
                },
                # Preserve legacy terminal behavior for callers that still
                # use ``task_goal``; structured AgentTask calls stay in the
                # controller loop and are explicitly continued there.
                terminate=legacy,
            )
        return self._error_result(outcome, legacy=legacy)


def make_delegation_tool(skill: str, runner: DelegationRunner) -> AgentTool:
    """Build the ``delegate-<skill>`` tool for *runner*."""
    return _DelegationAgentTool(skill=skill, runner=runner)


__all__ = [
    "AgentTask",
    "DelegationOutcome",
    "DelegationRunner",
    "DelegationStatus",
    "make_delegation_tool",
]
