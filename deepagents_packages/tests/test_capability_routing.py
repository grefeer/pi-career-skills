from __future__ import annotations

import deepagents_skills.skills as skills_module
from deepagents_skills.contracts import RunRequest
from deepagents_skills.models import ScriptedFakeChatModel
from deepagents_skills.run_state import HarnessState
from deepagents_skills.skills import build_subagent_spec

from pi_career_skills.agents.capabilities import CAPABILITY_REGISTRY
from pi_career_skills.context import ToolContext
from pi_career_skills.registry import build_career_tool_registry


class MarkerModel:
    pass


def test_subagent_spec_uses_capability_model_and_catalog() -> None:
    model = MarkerModel()

    spec = build_subagent_spec(
        "job-matching",
        model,
        build_career_tool_registry(),
        ToolContext(user_id="u", run_id="r", skill_name="job-matching"),
        object(),
    )

    assert spec["model"] is model
    assert [tool.name for tool in spec["tools"]] == list(
        CAPABILITY_REGISTRY["job-matching"].tool_names
    )


def test_supervisor_graph_routes_capability_model(monkeypatch) -> None:
    supervisor_model = ScriptedFakeChatModel()
    matching_model = ScriptedFakeChatModel()
    captured: dict[str, object] = {}

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(skills_module, "create_deep_agent", fake_create_deep_agent)
    skills_module.build_supervisor_graph(
        supervisor_model,
        build_career_tool_registry(),
        HarnessState.from_request(RunRequest(task="匹配岗位")),
        allowed_skills=("job-matching",),
        models={"job-matching": matching_model},
    )

    spec = captured["subagents"][0]
    assert spec["model"] is matching_model
