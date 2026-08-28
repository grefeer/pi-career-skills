"""Model factories for the deepagents harness.

Two providers:

* :func:`create_deepseek_chat_model` — DeepSeek via the OpenAI-compatible
  protocol (``langchain-openai.ChatOpenAI``), mirroring
  ``pi_career_skills.model_factory`` (same base URL, temperature=0, v4
  thinking disabled).
* :class:`ScriptedFakeChatModel` — an offline ``BaseChatModel`` that drives
  a deterministic supervisor → subagent → tool → structured-outcome loop
  without any API key or network.  Used for the ``faux`` smoke test.

The fake inspects the tools bound to each call (captured in
``bind_tools``) so it can tell the supervisor (has ``task``) apart from a
skill subagent (has the ``DelegationOutcome`` structured-output tool) and
respond accordingly.
"""

from __future__ import annotations

import os
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
DEEPSEEK_API_KEY_ENV: str = "DEEPSEEK_API_KEY"
DEFAULT_DEEPSEEK_MODEL: str = "deepseek-v4-flash"

#: Structured-output tool name the ToolStrategy derives from the schema
#: (``_SchemaSpec.name`` falls back to ``__name__``).  Shared by the faux
#: model so it can emit the final delegation outcome call.
STRUCTURED_OUTCOME_TOOL = "DelegationOutcome"


def get_deepseek_api_key() -> str | None:
    """Return the DeepSeek API key from the environment, or ``None``."""
    return os.environ.get(DEEPSEEK_API_KEY_ENV) or None


def create_deepseek_chat_model(
    model_id: str = DEFAULT_DEEPSEEK_MODEL,
    api_key: str | None = None,
) -> ChatOpenAI:
    """Build a DeepSeek ``ChatOpenAI`` (OpenAI-compatible protocol)."""
    return ChatOpenAI(
        model=model_id,
        base_url=DEEPSEEK_BASE_URL,
        api_key=api_key or get_deepseek_api_key(),
        temperature=0,
        # v4 hidden reasoning off, mirroring model_factory._thinking_extra_body.
        extra_body={"thinking": {"type": "disabled"}},
    )


class ScriptedFakeChatModel(BaseChatModel):
    """Deterministic offline chat model for the ``faux`` smoke test.

    Behavior driven by the tools bound to the current call:

    * supervisor (has ``task``): first turn issues a ``task`` call to
      ``career-planning``; after the task result returns a short summary.
    * skill subagent (has ``DelegationOutcome``): first turn calls
      ``build-preparation-plan`` (a pure, offline, artifact-producing
      handler); after the tool result emits the structured outcome call.

    The final run must pass the completion gate for ``career-planning`` —
    which ``build-preparation-plan`` satisfies offline — so the whole
    supervisor → delegate → evidence → completion pipeline is exercised
    without any API key or network.
    """

    #: Per-instance tool-name registry.  ``bind_tools`` on the base class
    #: raises NotImplementedError, so the fake records the bound tools and
    #: returns a fresh copy of itself (each agent's binding gets its own
    #: identity, so supervisor / subagent scripts never bleed into each other).
    _BOUND_BY_INSTANCE: dict[int, list[str]] = {}

    @property
    def _llm_type(self) -> str:  # pragma: no cover - trivial
        return "scripted-fake"

    def bind_tools(
        self,
        tools: Any,
        *,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Record bound tool names, then return a bound copy of the model."""
        del tool_choice, kwargs
        names: list[str] = []
        for tool in tools:
            if isinstance(tool, dict):
                names.append(str(tool.get("name", "")))
            else:
                names.append(str(getattr(tool, "name", "")))
        bound = self.model_copy(deep=False)
        self._BOUND_BY_INSTANCE[id(bound)] = names
        return bound

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        bound = self._BOUND_BY_INSTANCE.get(id(self), [])
        last = messages[-1] if messages else None
        has_task = "task" in bound
        has_outcome = STRUCTURED_OUTCOME_TOOL in bound

        if isinstance(last, ToolMessage):
            if has_outcome:
                # Subagent final turn after a business tool result.
                return self._structured_outcome(last)
            # Supervisor after a delegation result: final summary turn.
            return self._text_result(
                "已完成 job-discovery/career-planning 委托，证据已入库并给出总结。"
            )

        # First turn of an agent.
        if has_outcome:
            return self._business_tool_call()
        if has_task:
            return self._task_call()
        return self._text_result("done")

    # -- scripted responses --------------------------------------------

    def _text_result(self, text: str) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    def _tool_result(self, tool_calls: list[dict[str, Any]]) -> ChatResult:
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="", tool_calls=tool_calls))]
        )

    def _task_call(self) -> ChatResult:
        return self._tool_result(
            [
                {
                    "name": "task",
                    "args": {
                        "subagent_type": "career-planning",
                        "description": (
                            "基于目标岗位 AI 应用开发工程师生成求职准备计划，"
                            "调用 build-preparation-plan 产出 career_preparation_plan。"
                        ),
                    },
                    "id": "call-task-smoke-1",
                }
            ]
        )

    def _business_tool_call(self) -> ChatResult:
        bound = self._BOUND_BY_INSTANCE.get(id(self), [])
        if "build-preparation-plan" in bound:
            # career-planning: role-level target — pure + offline, produces a
            # career_preparation_plan artifact (see prompt rule 2).
            return self._tool_result(
                [
                    {
                        "name": "build-preparation-plan",
                        "args": {
                            "target_artifact_id": "role:AI应用开发工程师",
                            "focus_keywords": ["AI 应用开发", "微服务", "Java", "Spring Cloud"],
                            "time_budget_hours": 6,
                        },
                        "id": "call-plan-smoke-1",
                    }
                ]
            )
        if "read-skill-reference" in bound:
            # Other skills: the skill-reference read is pure + offline and
            # never produces a job-bearing artifact, so the smoke run stays
            # honest (those skills simply do not complete offline).
            return self._tool_result(
                [
                    {
                        "name": "read-skill-reference",
                        "args": {"skill_name": "job-discovery"},
                        "id": "call-ref-smoke-1",
                    }
                ]
            )
        return self._text_result("done")

    def _structured_outcome(self, last: ToolMessage) -> ChatResult:
        # Build a DelegationOutcome-compatible payload; refs may be empty —
        # the completion gate is decided from the EvidenceStore, not this JSON.
        bound = self._BOUND_BY_INSTANCE.get(id(self), [])
        skill = (
            "career-planning"
            if "build-preparation-plan" in bound
            else "job-discovery"
        )
        payload = {
            "skill": skill,
            "status": "success",
            "summary": "已生成求职准备计划并持久化 career_preparation_plan 证据。",
            "refs": [],
            "error_code": None,
            "action": "continue",
            "consumed_budget": None,
        }
        return self._tool_result(
            [
                {
                    "name": STRUCTURED_OUTCOME_TOOL,
                    "args": payload,
                    "id": "call-outcome-smoke-1",
                }
            ]
        )


__all__ = [
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_API_KEY_ENV",
    "DEFAULT_DEEPSEEK_MODEL",
    "STRUCTURED_OUTCOME_TOOL",
    "create_deepseek_chat_model",
    "get_deepseek_api_key",
    "ScriptedFakeChatModel",
]
