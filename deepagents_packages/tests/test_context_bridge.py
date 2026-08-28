from __future__ import annotations

import hashlib

from deepagents_skills.contracts import RunRequest
from deepagents_skills.controller import seed_artifact
from deepagents_skills.run_state import HarnessState
from deepagents_skills.tools_adapter import CareerLangchainTool
from pydantic import BaseModel

from pi_career_skills.agents.capabilities import CAPABILITY_REGISTRY
from pi_career_skills.context import ToolContext
from pi_career_skills.contracts import Artifact
from pi_career_skills.registry import CareerToolRegistry, ToolDefinition


def _seeded_page() -> Artifact:
    visible_text = (
        "Java 后端开发工程师\n"
        "岗位职责：负责Java服务开发。\n"
        "任职要求：熟悉Spring Boot。"
    )
    return Artifact(
        artifact_id="seed-page",
        artifact_type="public_job_page",
        tool_name="fetch-public-job-pages",
        source_url="https://example.com/seed-page",
        content_hash=hashlib.sha256(visible_text.encode()).hexdigest(),
        quality="jd_complete",
        content={"visible_text": visible_text},
    )


def _state() -> HarnessState:
    return HarnessState.from_request(
        RunRequest(
            task="根据已观察职位做匹配",
            allowed_skills=("job-discovery", "job-matching"),
            needed_skills=("job-matching",),
            private_context={"confirmed_profile_facts": [{"field": "skill", "value": "Java"}]},
        )
    )


def test_projected_metadata_includes_seeded_evidence() -> None:
    state = _state()
    seed_artifact(state.store, _seeded_page())

    metadata = state.projected_metadata("job-matching")

    assert metadata["observed_public_evidence"]
    assert metadata["observed_public_evidence"][0]["source_url"] == (
        "https://example.com/seed-page"
    )


def test_tool_refreshes_context_with_live_evidence_and_delegation_goal() -> None:
    state = _state()
    seed_artifact(state.store, _seeded_page())
    state.delegation_goals["job-matching"] = "请匹配北京的 Java 后端岗位"
    captured: dict[str, object] = {}

    class InputModel(BaseModel):
        value: int

    class OutputModel(BaseModel):
        ok: bool

    def handler(context: ToolContext, payload: InputModel) -> OutputModel:
        del payload
        captured.update(context.metadata)
        return OutputModel(ok=True)

    definition = ToolDefinition(
        name="capture-context",
        skill_name="job-matching",
        description="test context capture",
        input_model=InputModel,
        output_model=OutputModel,
        handler=handler,
    )
    registry = CareerToolRegistry()
    registry.register(definition)
    tool = CareerLangchainTool(
        definition,
        registry,
        ToolContext(
            user_id="user",
            run_id=state.kernel.run_id,
            attempt_id=state.kernel.attempt_id,
            skill_name="job-matching",
            metadata={},
        ),
        state=state,
    )

    tool._run(value=1)

    assert captured["task_goal"] == "请匹配北京的 Java 后端岗位"
    assert captured["observed_public_evidence"]
    assert captured["enforce_public_request_governor"] is True


def test_delegation_uses_capability_budget_and_restores_parent_tracker() -> None:
    state = _state()
    parent = state.tracker

    state.begin_delegation("job-matching", "对已观察岗位生成匹配报告")

    assert state.tracker is not parent
    assert state.tracker.limits.agent_turns == CAPABILITY_REGISTRY["job-matching"].default_budget["agent_turns"]
    assert state.delegation_goals["job-matching"] == "对已观察岗位生成匹配报告"

    state.end_delegation()

    assert state.tracker is parent
