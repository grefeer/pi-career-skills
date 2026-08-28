from __future__ import annotations

import json

from deepagents_skills.tools_adapter import CareerLangchainTool
from pydantic import BaseModel

from pi_career_skills.context import ToolContext
from pi_career_skills.errors import TOOL_SKILL_FORBIDDEN
from pi_career_skills.registry import CareerToolRegistry, ToolDefinition


class InputModel(BaseModel):
    value: int = 1


class OutputModel(BaseModel):
    ok: bool


def test_langchain_tool_rechecks_skill_isolation() -> None:
    definition = ToolDefinition(
        name="matching-only",
        skill_name="job-matching",
        description="test tool",
        input_model=InputModel,
        output_model=OutputModel,
        handler=lambda _context, _payload: OutputModel(ok=True),
    )
    registry = CareerToolRegistry()
    registry.register(definition)
    tool = CareerLangchainTool(
        definition,
        registry,
        ToolContext(user_id="u", run_id="r", skill_name="job-discovery"),
    )

    payload = json.loads(tool._run(value=1))

    assert payload["error_code"] == TOOL_SKILL_FORBIDDEN
