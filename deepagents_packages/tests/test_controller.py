"""Controller-level tests: wall-clock timeout must still succeed when the
store (the authority on completion) already satisfies the completion gates."""
from __future__ import annotations

import hashlib

from deepagents_skills.contracts import RunRequest
from deepagents_skills.controller import CareerRunController
from deepagents_skills.run_state import HarnessState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from pi_career_skills.contracts import Artifact


class NoopModel(BaseChatModel):
    """Never emits a tool call — the graph returns immediately."""

    @property
    def _llm_type(self) -> str:
        return "noop"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        del tools, tool_choice, kwargs
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])


def _seed_jd(state: HarnessState, n: int = 3) -> None:
    from pi_career_skills.runtime.context_projection import seed_artifact

    for i in range(n):
        url = f"https://career.example.edu.cn/job/{i}"
        body = "岗位职责：负责数据分析平台建设。任职要求：本科及以上，3年经验。"
        art = Artifact(
            artifact_id=hashlib.sha256(f"{url}".encode()).hexdigest(),
            artifact_type="public_job_page",
            tool_name="fetch-public-job-page",
            source_url=url,
            content_hash=hashlib.sha256(f"{url}{body}".encode()).hexdigest(),
            quality="jd_complete",
            content={"visible_text": body, "title": "数据分析师"},
        )
        seed_artifact(state.store, art)


def test_satisfy_from_store_marks_complete_skills() -> None:
    """The store checkers are authoritative: a seeded store of quality JDs
    marks job-discovery completed regardless of model output."""
    controller = CareerRunController(model=NoopModel())
    request = RunRequest(
        task="收集公开岗位",
        allowed_skills=("job-discovery",),
        needed_skills=("job-discovery",),
    )
    state = HarnessState.from_request(request)
    state.allowed_skills = {"job-discovery"}
    _seed_jd(state, n=3)

    controller._satisfy_from_store(state, request)

    assert "job-discovery" in state.kernel.completed_skills
    assert state.kernel.completed_skills.issuperset(request.needed_skills or ())
    assert state.store.job_bearing_artifacts()


def test_timeout_completion_decision_upgrades_to_succeeded() -> None:
    """Replicate the controller's wall-clock timeout branch: with a complete
    store and no hostile halt, waiting_user/wall_clock upgrades to succeeded."""
    controller = CareerRunController(model=NoopModel())
    request = RunRequest(
        task="收集公开岗位",
        allowed_skills=("job-discovery",),
        needed_skills=("job-discovery",),
    )
    state = HarnessState.from_request(request)
    state.allowed_skills = {"job-discovery"}
    _seed_jd(state, n=3)

    outcome_status = "waiting_user"
    outcome_code = "wall_clock_budget_exhausted"
    controller._satisfy_from_store(state, request)
    benign_halt = state.halt_code is None or state.halt_code in {
        "no_progress",
        "route_already_consumed",
        "delegation_retry_limit",
        "budget_exhausted",
        "wall_clock_budget_exhausted",
    }
    if (
        benign_halt
        and state.kernel.completed_skills.issuperset(request.needed_skills or ())
        and state.store.job_bearing_artifacts()
    ):
        outcome_status = "succeeded"
        outcome_code = None

    assert outcome_status == "succeeded"
    assert outcome_code is None


def test_timeout_completion_keeps_wall_clock_when_store_incomplete() -> None:
    """With an incomplete store the timeout path must keep the recoverable
    wall-clock outcome so auto-recovery can continue from the store."""
    controller = CareerRunController(model=NoopModel())
    request = RunRequest(
        task="收集公开岗位",
        allowed_skills=("job-discovery",),
        needed_skills=("job-discovery",),
    )
    state = HarnessState.from_request(request)
    state.allowed_skills = {"job-discovery"}

    outcome_status = "waiting_user"
    outcome_code = "wall_clock_budget_exhausted"
    controller._satisfy_from_store(state, request)
    benign_halt = state.halt_code is None or state.halt_code in {
        "no_progress",
        "route_already_consumed",
        "delegation_retry_limit",
        "budget_exhausted",
        "wall_clock_budget_exhausted",
    }
    if (
        benign_halt
        and state.kernel.completed_skills.issuperset(request.needed_skills or ())
        and state.store.job_bearing_artifacts()
    ):
        outcome_status = "succeeded"
        outcome_code = None

    assert outcome_status == "waiting_user"
    assert outcome_code == "wall_clock_budget_exhausted"
