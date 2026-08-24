"""Phase 5 gate tests — supervisor/skill scoping, delegation tools (§6.7).

Drives real agent loops with the faux provider: scripts are consumed FIFO,
one per model turn; a ``tool_calls`` script makes the loop execute that tool,
a text-only script ends the loop.  The fake runner records calls and returns
scripted outcomes — no network, no LLM.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from pi_ai import ToolCall
from pi_ai.providers.faux import FAUX_MODEL, FauxScript, clear_scripts, push_script
from pi_career_skills.agents.delegation_tools import (
    DelegationOutcome,
    make_delegation_tool,
)
from pi_career_skills.agents.factory import build_skill_agent, build_supervisor_agent
from pi_career_skills.agents.prompts import (
    CAREER_PLANNING_PROMPT,
    JOB_DISCOVERY_PROMPT,
    JOB_MATCHING_PROMPT,
    PROMPT_HASHES,
    RESUME_TAILORING_PROMPT,
    SUPERVISOR_PROMPT,
)
from pi_career_skills.context import ToolContext
from pi_career_skills.errors import (
    DELEGATION_SKILL_ALREADY_SUCCEEDED,
    TARGET_EVIDENCE_NOT_FOUND,
    CareerToolError,
)
from pi_career_skills.registry import TOOL_CATALOG_BY_SKILL

SUPERVISOR_DELEGATE_TOOLS = frozenset(
    {
        "delegate-job-discovery",
        "delegate-job-matching",
        "delegate-resume-tailoring",
        "delegate-career-planning",
    }
)
#: Representative business tool names the supervisor must never see.
BUSINESS_TOOL_NAMES = frozenset(
    {
        "fetch-public-job-pages",
        "match-observed-jobs",
        "build-resume-tailoring-brief",
        "build-preparation-plan",
    }
)

_CTX = ToolContext(user_id="user-1", run_id="run-1")


@pytest.fixture(autouse=True)
def _clear_faux_scripts() -> None:
    clear_scripts()
    yield
    clear_scripts()


def _fake_runner(
    calls: list[tuple[str, dict[str, Any]]],
    outcome: DelegationOutcome,
) -> Any:
    def runner(task_goal: str, params: dict[str, Any]) -> DelegationOutcome:
        calls.append((task_goal, params))
        return outcome

    return runner


def _visible_text(agent: Any) -> str:
    """Join the model-visible text of all toolResult messages in the state."""
    parts: list[str] = []
    for message in agent.state.messages:
        if getattr(message, "role", None) == "toolResult":
            for block in message.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Scoping gates
# ---------------------------------------------------------------------------


def test_supervisor_scoping() -> None:
    """Supervisor sees ONLY the four delegate tools — no business tools."""
    agent = build_supervisor_agent(FAUX_MODEL, _fake_runner([], DelegationOutcome(skill="job-discovery", status="succeeded")))
    names = {tool.name for tool in agent.state.tools}
    assert names == SUPERVISOR_DELEGATE_TOOLS
    assert names.isdisjoint(BUSINESS_TOOL_NAMES)


def test_skill_scoping() -> None:
    """Each skill agent sees exactly its own catalog (10/1/1/1), nothing else."""
    counts = {skill: len(names) for skill, names in TOOL_CATALOG_BY_SKILL.items()}
    assert counts == {
        "job-discovery": 10,
        "job-matching": 1,
        "resume-tailoring": 1,
        "career-planning": 1,
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
        assert set(names).isdisjoint(other_skill_names)


# ---------------------------------------------------------------------------
# Delegation flows (real agent loop + faux provider)
# ---------------------------------------------------------------------------


async def test_single_skill_delegation() -> None:
    """One delegate-job-discovery turn: goal reaches the runner, bounded
    summary is the model-visible content."""
    calls: list[tuple[str, dict[str, Any]]] = []
    outcome = DelegationOutcome(
        skill="job-discovery",
        status="succeeded",
        summary="共找到 3 个匹配职位，来自 2 个公开来源。",
        refs=[{"artifact_id": "a-1", "source_url": "https://example.com/job/1"}],
    )
    agent = build_supervisor_agent(FAUX_MODEL, _fake_runner(calls, outcome))
    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="delegate-job-discovery",
                    arguments={"task_goal": "找 Java 后端岗位"},
                )
            ]
        )
    )
    await agent.prompt("帮我找 Java 后端岗位")
    assert calls == [("找 Java 后端岗位", {"task_goal": "找 Java 后端岗位"})]
    assert "共找到 3 个匹配职位" in _visible_text(agent)


async def test_multi_skill_delegation() -> None:
    """Two delegations via prompt + continue_: both skills reach the runner."""
    calls: list[tuple[str, dict[str, Any]]] = []
    outcomes = [
        DelegationOutcome(
            skill="job-discovery", status="succeeded", summary="发现 3 个匹配职位"
        ),
        DelegationOutcome(
            skill="job-matching", status="succeeded", summary="排序完成：3 个推荐"
        ),
    ]

    def runner(task_goal: str, params: dict[str, Any]) -> DelegationOutcome:
        calls.append((task_goal, params))
        return outcomes.pop(0)

    agent = build_supervisor_agent(FAUX_MODEL, runner)
    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="delegate-job-discovery",
                    arguments={"task_goal": "找 Java 后端岗位"},
                )
            ]
        )
    )
    await agent.prompt("帮我找 Java 后端岗位")
    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="call_2",
                    name="delegate-job-matching",
                    arguments={"task_goal": "匹配 Java 后端岗位"},
                )
            ]
        )
    )
    await agent.continue_()
    assert calls == [
        ("找 Java 后端岗位", {"task_goal": "找 Java 后端岗位"}),
        ("匹配 Java 后端岗位", {"task_goal": "匹配 Java 后端岗位"}),
    ]
    visible = _visible_text(agent)
    assert "发现 3 个匹配职位" in visible
    assert "排序完成：3 个推荐" in visible


# ---------------------------------------------------------------------------
# Controlled error paths (§6.7 — never text-fake success)
# ---------------------------------------------------------------------------


async def test_duplicate_delegation_error() -> None:
    """delegation_skill_already_succeeded: literal code in content, no fake
    success, terminate=False (loop may continue with another skill)."""

    def runner(task_goal: str, params: dict[str, Any]) -> DelegationOutcome:
        del task_goal, params
        return DelegationOutcome(
            skill="job-discovery",
            status="error",
            error_code=DELEGATION_SKILL_ALREADY_SUCCEEDED,
        )

    tool = make_delegation_tool("job-discovery", runner)
    result = await tool.execute("call_1", {"task_goal": "找 Java 后端岗位"}, None, None)
    assert result.terminate is False
    text = result.content[0].text
    assert DELEGATION_SKILL_ALREADY_SUCCEEDED in text
    assert text == (
        f"技能 job-discovery 已完成委托，无需重复执行"
        f"（{DELEGATION_SKILL_ALREADY_SUCCEEDED}）"
    )


async def test_missing_evidence_error() -> None:
    """target_evidence_not_found: literal code in content, no fake success,
    terminate=False."""

    def runner(task_goal: str, params: dict[str, Any]) -> DelegationOutcome:
        del task_goal, params
        return DelegationOutcome(
            skill="job-matching",
            status="error",
            error_code=TARGET_EVIDENCE_NOT_FOUND,
        )

    tool = make_delegation_tool("job-matching", runner)
    result = await tool.execute("call_1", {"task_goal": "匹配岗位"}, None, None)
    assert result.terminate is False
    text = result.content[0].text
    assert TARGET_EVIDENCE_NOT_FOUND in text
    assert text == (
        f"缺少已观察的职位证据，无法执行 job-matching"
        f"（{TARGET_EVIDENCE_NOT_FOUND}）"
    )


async def test_runner_career_tool_error_is_controlled() -> None:
    """A runner raising CareerToolError maps to error status with its code."""

    def runner(task_goal: str, params: dict[str, Any]) -> DelegationOutcome:
        del task_goal, params
        raise CareerToolError("some_policy_code", "policy violation")

    tool = make_delegation_tool("resume-tailoring", runner)
    result = await tool.execute("call_1", {"task_goal": "改简历"}, None, None)
    assert result.terminate is False
    assert "some_policy_code" in result.content[0].text
    assert result.details == {
        "skill": "resume-tailoring",
        "status": "error",
        "error_code": "some_policy_code",
    }


# ---------------------------------------------------------------------------
# Factory edges + prompt provenance
# ---------------------------------------------------------------------------


def test_unknown_skill_raises() -> None:
    with pytest.raises(ValueError):
        build_skill_agent("nope", FAUX_MODEL, _CTX)


#: sha256 (UTF-8) of the curated migration prompts, pinned at port time.
_VERBATIM_HASHES = {
    "supervisor": "38d6d67087e13505bd82d06f587322dd7fd66a50775fb9ab435c9330f5d3e075",
    "job-discovery": "677ecab69aab6741b4e6938689fced40d0af48cecd573255ca97694e5fc3769b",
    "job-matching": "b1ec5fe666720c42d61a0d55d09294d799c929cea5d27781e34dd77f2cc80183",
    "resume-tailoring": "36bd80a0b851dcb6cde7c6612d3266bb4e846ed8e519a1de489bd11557d471e5",
    "career-planning": "6ca185c654d95a30e8cbe72f65b6250bb8879c91a62cf832a644c066f71ad9c8",
}


def test_prompt_hashes() -> None:
    """PROMPT_HASHES covers exactly the five prompts, computed from the
    actual module strings, and matches the pinned migration contract."""
    prompts = {
        "supervisor": SUPERVISOR_PROMPT,
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
