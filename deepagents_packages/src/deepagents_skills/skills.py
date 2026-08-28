"""Supervisor + 4 skill subagents on deepagents ``create_deep_agent``.

The agent structure is unchanged from pi-career-skills (thin supervisor +
one subagent per skill, MIGRATION.md §2): the supervisor sees only the
``task`` tool (added by deepagents' SubAgentMiddleware), each skill subagent
sees exactly its own catalog plus the shared ``read-skill-reference``, and
messages are never shared across subagents.

The supervisor system prompt is the deepagents adaptation of pi's
``SUPERVISOR_PROMPT`` (delegation via ``task(subagent_type=..., description=...)``
instead of the pi ``delegate-<skill>`` tools).  Skill prompts reuse
``load_archived_skill_prompt`` verbatim, plus a structured completion
contract telling the model to finish by emitting ``DelegationOutcome``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from deepagents import HarnessProfile, register_harness_profile
from deepagents.graph import create_deep_agent
from deepagents.middleware import SubAgent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel

from pi_career_skills.agents.capabilities import CAPABILITY_REGISTRY
from pi_career_skills.agents.contracts import DelegationOutcome
from pi_career_skills.agents.prompts import (
    CAREER_PLANNING_PROMPT,
    JOB_DISCOVERY_PROMPT,
    JOB_MATCHING_PROMPT,
    RESUME_TAILORING_PROMPT,
    load_archived_skill_prompt,
)
from pi_career_skills.context import ToolContext
from pi_career_skills.registry import CareerToolRegistry

from .middleware.harness import build_middleware_stack
from .run_state import HarnessState
from .tools_adapter import build_skill_tools

#: deepagents built-in tools that must never reach the model.  Subagents were
#: observed burning their 40-model-call budget looping on the filesystem tools
#: (``ls /evidence`` …) instead of calling their business catalog, because the
#: evidence lives in projected metadata, not on disk.  Excluding them (the
#: official ``HarnessProfile.excluded_tools`` mechanism) leaves the supervisor
#: with only ``task`` and each subagent with only its business tools.
_EXCLUDED_BUILTIN_TOOLS: frozenset[str] = frozenset(
    {
        "write_todos",
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "execute",
    }
)

#: The deepseek chat models are ChatOpenAI instances, so deepagents derives
#: provider="openai" from the class (not the deepseek base_url).  Register
#: under both the canonical provider:model key and the provider-wide key so
#: any deepseek model name resolves.
for _harness_key in ("openai:deepseek-v4-flash", "openai"):
    register_harness_profile(
        _harness_key,
        HarnessProfile(excluded_tools=_EXCLUDED_BUILTIN_TOOLS),
    )

#: Curated prompt per skill — single source: pi_career_skills.agents.prompts.
_SKILL_PROMPTS: dict[str, str] = {
    "job-discovery": JOB_DISCOVERY_PROMPT,
    "job-matching": JOB_MATCHING_PROMPT,
    "resume-tailoring": RESUME_TAILORING_PROMPT,
    "career-planning": CAREER_PLANNING_PROMPT,
}

#: Subagent descriptions surfaced to the supervisor in the ``task`` tool.
_SKILL_DESCRIPTIONS: dict[str, str] = {
    "job-discovery": (
        "收集少量、可追溯的公开招聘证据（职位页面 + 结构化 JD）。"
        "matching/tailoring/planning 需要岗位证据时先委托本技能。"
    ),
    "job-matching": (
        "基于候选人已确认事实与本次运行已观察的职位证据，产出透明匹配排序。"
        "需要已持久化的 job-discovery 证据作为输入。"
    ),
    "resume-tailoring": (
        "针对一个已观察目标 JD，生成不可虚构、可审阅的简历修改建议。"
        "需要已持久化的 job-discovery 证据作为输入。"
    ),
    "career-planning": (
        "基于已观察目标 JD（或明确的角色级目标）生成可执行的求职准备计划。"
    ),
}

#: Structured completion contract appended to every skill prompt.  The
#: subagent must finish by emitting ``DelegationOutcome`` (the ToolStrategy
#: output tool); the supervisor consumes that JSON as the task result.
_DEEPAGENTS_COMPLETION_CONTRACT = """

## 完成契约（deepagents 结构化返回）
任务完成或必须停止时，调用 DelegationOutcome 工具返回结构化结果：
- skill: 当前技能名；
- status: success（已产出持久化交付物）/ partial（部分证据支持有界结论）/ needs_user（需要用户澄清或补充证据）/ retryable（可调整约束后重试）/ blocked（安全门或资源限制阻止）/ failed（失败）；
- summary: 中文简短总结；
- refs: 本环节已持久化证据的 artifact_id/source_url/content_hash 列表（没有则为空列表）；
- error_code: 失败或阻止时的错误码（成功为 null）；
- action: continue / ask_user / retry / stop 之一。
job-discovery 特别注意（只对该技能生效）：收集证据必须达到最低门槛再返回——
至少 3 条质量合格的岗位证据（browse-public-job-page 得到 jd_complete 的公开页面，
或含完整职责/要求的结构化岗位）；单页内容不完整（jd_partial）不算合格，
应继续尝试其他种子链接或候选页面，不要因一两页部分内容就停止。
证据不足时应返回 status=partial（不要伪装成 success）。
拿到足够交付物后立即返回结构化结果，不要重复调用已成功的业务工具。
"""

_SUPERVISOR_PROMPT = (
    "You are the career supervisor. You must delegate every user request to the matching "
    "reviewed career Skill with the official `task` tool (subagent_type must be one of: "
    "job-discovery, job-matching, resume-tailoring, career-planning); do not answer from "
    "general knowledge or invent job evidence.\n\n"
    "Workflow:\n"
    "1. Decide which Skill(s) the request needs. A planning / matching / tailoring "
    "request without observed job evidence requires job-discovery FIRST to gather "
    "traceable public evidence; only then delegate the downstream Skill.\n"
    "2. Delegate one Skill at a time with `task`. A Skill whose result is blocked or "
    "failed (e.g. missing evidence) means you must re-route — change the delegation "
    "arguments or first delegate job-discovery to gather evidence — never stop on it.\n"
    "3. Keep delegating until EVERY required Skill has returned a usable deliverable "
    "(task status success/partial with evidence). Do NOT give a FINAL answer while a "
    "required Skill is still outstanding.\n"
    "4. When all required Skills are done, produce your FINAL answer summarizing the "
    "result for the user and stop calling tools. Do not re-delegate a Skill that "
    "already returned a usable deliverable."
)

SUPERVISOR_PROMPT = _SUPERVISOR_PROMPT


def skill_system_prompt(skill_name: str) -> str:
    """Archived skill contract + curated prompt + deepagents completion clause."""
    return (
        load_archived_skill_prompt(skill_name, _SKILL_PROMPTS[skill_name])
        + _DEEPAGENTS_COMPLETION_CONTRACT
    )


def build_subagent_spec(
    skill_name: str,
    model: BaseChatModel,
    registry: CareerToolRegistry,
    context: ToolContext,
    state: HarnessState,
    *,
    summarization: bool = False,
) -> SubAgent:
    """One skill subagent: its catalog, curated prompt and structured output."""
    capability = CAPABILITY_REGISTRY.require(skill_name)
    tools = build_skill_tools(skill_name, registry, context, state)
    return {
        "name": skill_name,
        "description": capability.description,
        "system_prompt": skill_system_prompt(skill_name),
        "tools": tools,
        "model": model,
        "middleware": build_middleware_stack(
            skill_name=skill_name, model=model, summarization=summarization
        ),
        # ToolStrategy (not AutoStrategy) so both ChatOpenAI and the scripted
        # faux model emit the deterministic `DelegationOutcome` tool call.
        "response_format": ToolStrategy(schema=DelegationOutcome),
    }


def build_supervisor_graph(
    model: BaseChatModel,
    registry: CareerToolRegistry,
    state: HarnessState,
    *,
    allowed_skills: tuple[str, ...] | None = None,
    private_context: dict[str, Any] | None = None,
    task_goal: str | None = None,
    models: dict[str, BaseChatModel] | None = None,
    summarization: bool = False,
) -> Any:
    """Build the compiled supervisor graph for one attempt.

    Built per attempt so each skill subagent's ``ToolContext`` carries the
    fresh ``attempt_id`` (mirrors pi's per-attempt agent construction).
    """
    skills = list(_SKILL_DESCRIPTIONS)
    if allowed_skills is not None:
        allowed = set(allowed_skills)
        skills = [s for s in skills if s in allowed]

    metadata = dict(private_context or {})
    if task_goal:
        # Mirrors pi's ``ctx.metadata["task_goal"] = task.objective``; the
        # role-level planning path keys off it (career_planning.py).
        metadata["task_goal"] = task_goal
    base_context = ToolContext(
        user_id=state.kernel.synthetic_user_id,
        run_id=state.kernel.run_id,
        attempt_id=state.kernel.attempt_id,
        skill_name=None,
        metadata=metadata,
    )
    specs = [
        build_subagent_spec(
            skill_name=skill,
            model=(models or {}).get(skill, model),
            registry=registry,
            context=replace(base_context, skill_name=skill),
            state=state,
            summarization=summarization,
        )
        for skill in skills
    ]
    return create_deep_agent(
        model=model,
        system_prompt=_SUPERVISOR_PROMPT,
        tools=[],  # business tools are never granted to the supervisor
        middleware=build_middleware_stack(
            skill_name=None, model=model, summarization=summarization
        ),
        subagents=specs,
        name="career-supervisor",
    )


__all__ = [
    "SUPERVISOR_PROMPT",
    "build_subagent_spec",
    "build_supervisor_graph",
    "skill_system_prompt",
]
