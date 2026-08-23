"""Tests for tool_adapter.py — AgentTool factory, invoke entries, isolation.

Covers: the sequential AgentTool success path through a real handler
(extract-observed-job-details), input/output validation, unknown tools,
blocked-vs-failed error mapping, message redaction, and skill isolation.
No test touches the network.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, Field

from pi_ai import TextContent
from pi_career_skills.context import ToolContext
from pi_career_skills.contracts import ToolObservation
from pi_career_skills.errors import (
    CAPTCHA,
    INVALID_TOOL_INPUT,
    INVALID_TOOL_OUTPUT,
    LOGIN_REQUIRED,
    TOOL_EXECUTION_FAILED,
    TOOL_SKILL_FORBIDDEN,
    UNKNOWN_TOOL,
    CareerToolError,
)
from pi_career_skills.registry import (
    CareerToolRegistry,
    ToolDefinition,
    build_career_tool_registry,
)
from pi_career_skills.tool_adapter import (
    bound_content,
    invoke_tool,
    make_agent_tool,
)

# ---------------------------------------------------------------------------
# Fixture tools (pure, deterministic — never touch the network)
# ---------------------------------------------------------------------------


class _EchoInput(BaseModel):
    value: str = Field(min_length=1)


class _EchoOutput(BaseModel):
    echo: str


def _echo_handler(context: ToolContext, payload: _EchoInput) -> dict[str, Any]:
    del context
    return {"echo": payload.value}


def _blocked_handler(context: ToolContext, payload: _EchoInput) -> dict[str, Any]:
    del context, payload
    raise CareerToolError(CAPTCHA, "请完成验证码验证")


def _crash_handler(context: ToolContext, payload: _EchoInput) -> dict[str, Any]:
    del context, payload
    raise RuntimeError("boom")


def _bad_output_handler(context: ToolContext, payload: _EchoInput) -> dict[str, Any]:
    del context, payload
    return {"echo": 123}  # int is not a str -> output validation fails


def _secret_handler(context: ToolContext, payload: _EchoInput) -> dict[str, Any]:
    del context, payload
    raise CareerToolError(
        LOGIN_REQUIRED,
        "login https://alice:hunter2@example.com/login "
        "Bearer sk-live-abc123 token=XYZ99QQ",
    )


def _test_registry() -> CareerToolRegistry:
    """A registry with echo + failure-path tools on the job-matching skill."""
    registry = CareerToolRegistry()
    for name, handler in (
        ("echo-tool", _echo_handler),
        ("blocked-tool", _blocked_handler),
        ("crash-tool", _crash_handler),
        ("bad-output-tool", _bad_output_handler),
        ("secret-tool", _secret_handler),
    ):
        registry.register(
            ToolDefinition(
                name=name,
                skill_name="job-matching",
                description=f"test {name}",
                input_model=_EchoInput,
                output_model=_EchoOutput,
                handler=handler,
            )
        )
    return registry


def _scoped_context(skill_name: str | None = "job-matching") -> ToolContext:
    return ToolContext(
        user_id="u1",
        run_id="r1",
        attempt_id="a1",
        skill_name=skill_name,
        metadata={},
    )


# ---------------------------------------------------------------------------
# bound_content
# ---------------------------------------------------------------------------


def test_bound_content_limits() -> None:
    """Top-level fields capped at 40; strings at 12k; lists at 20 items."""
    # Named fields come first — the 40-field cap applies in insertion order.
    value = {
        "long": "x" * 20_000,
        "items": [{"s": "y" * 5_000} for _ in range(30)],
        "deep": {"nested": "z" * 20_000},
        "scalar": None,
        "flag": True,
        **{f"k{i}": i for i in range(50)},
    }
    bounded = bound_content(value)
    assert len(bounded) == 40
    assert len(bounded["long"]) == 12_000
    assert len(bounded["items"]) == 20
    # Mapping items inside lists recurse into bound_content, so their strings
    # get the 12_000 top-level cap — 5_000 stays untruncated.
    assert len(bounded["items"][0]["s"]) == 5_000
    assert len(bounded["deep"]["nested"]) == 12_000
    assert bounded["scalar"] is None
    assert bounded["flag"] is True


# ---------------------------------------------------------------------------
# make_agent_tool — real handler success path
# ---------------------------------------------------------------------------


def _evidence_pool() -> list[dict[str, Any]]:
    """One observed public page artifact, shaped like the harness projection."""
    return [
        {
            "artifact_id": "ev-1",
            "artifact_type": "public_job_page",
            "source_url": "https://example.com/job/1",
            "content_hash": "a" * 64,
            "visible_text": (
                "<html><head><title>后端工程师 - 某大学就业信息网</title></head>"
                "<body>岗位职责：负责后端服务设计与开发，参与系统架构评审。</p>"
                "<p>任职要求：计算机相关专业本科及以上学历，3 年以上工作经验，"
                "熟悉 Python 与 MySQL。</body></html>"
            ),
        }
    ]


def _discovery_context() -> ToolContext:
    return ToolContext(
        user_id="u1",
        run_id="r1",
        attempt_id="a1",
        skill_name="job-discovery",
        metadata={"observed_public_evidence": _evidence_pool()},
    )


async def test_make_agent_tool_success_real_handler() -> None:
    """extract-observed-job-details through a real handler -> succeeded."""
    registry = build_career_tool_registry()
    definition = registry.get("extract-observed-job-details")
    assert definition is not None
    tool = make_agent_tool(definition, _discovery_context())

    assert tool.name == "extract-observed-job-details"
    assert tool.label == "extract-observed-job-details"
    assert tool.execution_mode == "sequential"
    assert isinstance(tool.parameters, dict)

    result = await tool.execute("call-1", {"artifact_id": "ev-1"})
    assert result.terminate is False
    assert len(result.content) == 1
    assert isinstance(result.content[0], TextContent)
    assert isinstance(result.details, ToolObservation)
    assert result.details.status == "succeeded"
    assert result.details.tool_call_id == "call-1"
    assert result.details.output is not None
    assert result.details.output.get("source_artifact_id") == "ev-1"
    # Model-visible content is the bounded output JSON.
    assert '"source_artifact_id": "ev-1"' in result.content[0].text


async def test_make_agent_tool_invalid_input() -> None:
    """Missing required param -> invalid_tool_input observation."""
    tool = make_agent_tool(
        _test_registry().get("echo-tool"), _scoped_context()
    )
    result = await tool.execute("call-2", {"value": ""})
    obs = result.details
    assert obs.status == "failed"
    assert obs.error_code == INVALID_TOOL_INPUT


async def test_make_agent_tool_invalid_output() -> None:
    """Handler output that fails the output model -> invalid_tool_output."""
    tool = make_agent_tool(
        _test_registry().get("bad-output-tool"), _scoped_context()
    )
    result = await tool.execute("call-3", {"value": "x"})
    obs = result.details
    assert obs.status == "failed"
    assert obs.error_code == INVALID_TOOL_OUTPUT


async def test_make_agent_tool_blocked_error_code() -> None:
    """CareerToolError(captcha) -> status=blocked with the stable code."""
    tool = make_agent_tool(
        _test_registry().get("blocked-tool"), _scoped_context()
    )
    result = await tool.execute("call-4", {"value": "x"})
    obs = result.details
    assert obs.status == "blocked"
    assert obs.error_code == CAPTCHA


async def test_make_agent_tool_generic_exception() -> None:
    """Raw exception -> failed with tool_execution_failed."""
    tool = make_agent_tool(
        _test_registry().get("crash-tool"), _scoped_context()
    )
    result = await tool.execute("call-5", {"value": "x"})
    obs = result.details
    assert obs.status == "failed"
    assert obs.error_code == TOOL_EXECUTION_FAILED
    assert "boom" in obs.error_message


async def test_redaction_strips_credentials_from_error() -> None:
    """Credential URL userinfo / Bearer / token never reach the model."""
    tool = make_agent_tool(_test_registry().get("secret-tool"), _scoped_context())
    result = await tool.execute("call-6", {"value": "x"})
    obs = result.details
    assert obs.status == "blocked"
    assert obs.error_code == LOGIN_REQUIRED
    assert "alice:hunter2" not in obs.error_message
    assert "sk-live-abc123" not in obs.error_message
    assert "XYZ99QQ" not in obs.error_message
    assert "alice" not in result.content[0].text


# ---------------------------------------------------------------------------
# invoke_tool — unknown tools + skill isolation
# ---------------------------------------------------------------------------


async def test_invoke_tool_unknown_tool() -> None:
    obs = await invoke_tool(
        _test_registry(), _scoped_context(), "no-such-tool", "call-7", {}
    )
    assert obs.status == "failed"
    assert obs.error_code == UNKNOWN_TOOL


async def test_invoke_tool_skill_mismatch() -> None:
    """job-matching context calling a job-discovery tool is forbidden."""
    registry = build_career_tool_registry()
    obs = await invoke_tool(
        registry,
        _scoped_context(skill_name="job-matching"),
        "extract-observed-job-details",
        "call-8",
        {"artifact_id": "ev-1"},
    )
    assert obs.status == "failed"
    assert obs.error_code == TOOL_SKILL_FORBIDDEN


async def test_invoke_tool_unscoped_context_forbidden() -> None:
    """skill_name=None (supervisor) may not call business tools."""
    registry = build_career_tool_registry()
    obs = await invoke_tool(
        registry,
        _scoped_context(skill_name=None),
        "extract-observed-job-details",
        "call-9",
        {"artifact_id": "ev-1"},
    )
    assert obs.status == "failed"
    assert obs.error_code == TOOL_SKILL_FORBIDDEN


async def test_invoke_tool_skill_scoped_succeeds() -> None:
    """Correct skill scope passes isolation and runs the real handler."""
    registry = build_career_tool_registry()
    obs = await invoke_tool(
        registry,
        _discovery_context(),
        "extract-observed-job-details",
        "call-10",
        {"artifact_id": "ev-1"},
    )
    assert obs.status == "succeeded"
    assert obs.output is not None
    assert obs.output.get("source_artifact_id") == "ev-1"


# ---------------------------------------------------------------------------
# registry.invoke — trusted-kernel sync path
# ---------------------------------------------------------------------------


def test_registry_invoke_kernel_path_skips_isolation() -> None:
    """The kernel entry runs unscoped contexts (deterministic fallback)."""
    registry = _test_registry()
    obs = registry.invoke(
        "echo-tool", _scoped_context(skill_name=None), {"value": "hi"}
    )
    assert obs.status == "succeeded"
    assert obs.output == {"echo": "hi"}


def test_registry_invoke_unknown_tool_observation() -> None:
    obs = _test_registry().invoke("missing", _scoped_context(), {})
    assert obs.status == "failed"
    assert obs.error_code == UNKNOWN_TOOL


@pytest.mark.parametrize(
    "code",
    [LOGIN_REQUIRED, CAPTCHA, "anti_bot", "needs_manual_review", "unsafe_public_url"],
)
def test_registry_invoke_blocked_codes_map_to_blocked(code: str) -> None:
    """Blocked codes surface as status=blocked through the kernel path too."""

    def _handler(ctx: Any, payload: Any) -> dict[str, Any]:
        del ctx, payload
        raise CareerToolError(code, f"blocked by {code}")

    registry = CareerToolRegistry()
    registry.register(
        ToolDefinition(
            name="blocked-param",
            skill_name="job-matching",
            description="blocked param tool",
            input_model=_EchoInput,
            output_model=_EchoOutput,
            handler=_handler,
        )
    )
    obs = registry.invoke("blocked-param", _scoped_context(), {"value": "x"})
    assert obs.status == "blocked"
    assert obs.error_code == code
