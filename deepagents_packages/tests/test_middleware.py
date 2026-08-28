from __future__ import annotations

from deepagents_skills.contracts import RunRequest
from deepagents_skills.middleware.harness import EvidenceMiddleware
from deepagents_skills.run_state import HarnessState
from langchain_core.messages import ToolMessage
from langgraph.constants import END
from langgraph.types import Command

from pi_career_skills.contracts import ToolObservation

BLOCKED_SOURCE_CODE = "anti_bot_challenge"


def _state() -> HarnessState:
    return HarnessState.from_request(
        RunRequest(
            task="收集公开岗位",
            allowed_skills=("job-discovery",),
            needed_skills=("job-discovery",),
        )
    )


def _blocked_call(state: HarnessState, index: int):
    call_id = f"blocked-{index}"
    state.record_observation(
        call_id,
        ToolObservation(
            tool_name="fetch-public-job-page",
            status="failed",
            error_code=BLOCKED_SOURCE_CODE,
            error_message="blocked",
            tool_call_id=call_id,
        ),
    )
    middleware = EvidenceMiddleware(skill_name="job-discovery")
    return middleware._handle_business_result(
        state,
        {"id": call_id, "name": "fetch-public-job-page", "args": {"url": f"https://example.com/{index}"}},
        f"params-{index}",
        ToolMessage(content="blocked", tool_call_id=call_id),
    )


def test_repeated_blocked_source_terminates_active_delegation() -> None:
    state = _state()

    _blocked_call(state, 1)
    result = _blocked_call(state, 2)

    assert isinstance(result, Command)
    assert result.goto == END
    assert result.update["messages"]
    assert state.halt_code == BLOCKED_SOURCE_CODE


def _task_tool_call(skill: str = "job-discovery") -> dict:
    return {
        "name": "task",
        "args": {"subagent_type": skill, "description": "d"},
        "id": "c1",
    }


def _killed_task_result() -> Command:
    """A task result with no structured DelegationOutcome (the subagent was
    killed by the model-call budget)."""
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content="Model call limits exceeded: thread limit (40/40), run limit (40/40)",
                    tool_call_id="c1",
                )
            ]
        }
    )


def test_killed_delegation_counts_and_halts_on_second_kill() -> None:
    """A delegation that ends without a structured outcome (killed by the
    model-call budget) must count per skill; the second kill records a hard
    halt so the supervisor stops re-delegating into the dead end."""
    state = _state()
    state.allowed_skills = {"job-discovery"}
    middleware = EvidenceMiddleware(skill_name=None)
    call = _task_tool_call()

    middleware._handle_delegation(state, call, "h", _killed_task_result())
    assert state.killed_delegation_counts == {"job-discovery": 1}
    assert state.halt_code is None

    middleware._handle_delegation(state, call, "h", _killed_task_result())
    assert state.killed_delegation_counts == {"job-discovery": 2}
    assert state.halt_code == "budget_exhausted"


def test_structured_partial_does_not_count_as_kill() -> None:
    """A structured DelegationOutcome (even partial) is a real delegation
    result and must never count toward the kill limit."""
    import json

    state = _state()
    state.allowed_skills = {"job-discovery"}
    middleware = EvidenceMiddleware(skill_name=None)
    call = _task_tool_call()
    partial = Command(
        update={
            "messages": [
                ToolMessage(
                    content=json.dumps(
                        {
                            "skill": "job-discovery",
                            "status": "partial",
                            "summary": "部分证据",
                            "refs": [],
                            "error_code": None,
                            "action": "continue",
                        },
                        ensure_ascii=False,
                    ),
                    tool_call_id="c1",
                )
            ]
        }
    )

    middleware._handle_delegation(state, call, "h", partial)
    middleware._handle_delegation(state, call, "h", partial)
    assert state.killed_delegation_counts == {}
    assert state.halt_code is None
