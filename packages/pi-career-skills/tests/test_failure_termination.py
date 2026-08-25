from __future__ import annotations

from types import SimpleNamespace

from pi_career_skills.contracts import ToolObservation
from pi_career_skills.runtime.agent_hooks import build_controller_hooks
from pi_career_skills.runtime.budgets import BudgetLimits, BudgetTracker, ToolCallGuard
from pi_career_skills.runtime.events import EventLogger
from pi_career_skills.runtime.evidence import EvidenceStore


def _hooks() -> tuple[object, list[tuple[str, str] | None]]:
    halt_box: list[tuple[str, str] | None] = [None]
    hooks = build_controller_hooks(
        tracker=BudgetTracker(BudgetLimits()),
        guard=ToolCallGuard(),
        store=EvidenceStore(),
        event_log=EventLogger(run_id="test"),
        halt_box=halt_box,
    )
    return hooks, halt_box


def _ctx(error_code: str, name: str = "fetch-public-job-page") -> dict[str, object]:
    return {
        "tool_call": SimpleNamespace(name=name),
        "args": {"url": "https://blocked.example/job/1"},
        "result": SimpleNamespace(
            details=ToolObservation(
                tool_name=name,
                status="failed",
                error_code=error_code,
            )
        ),
    }


def test_repeated_anti_bot_stops_route_without_artifacts() -> None:
    hooks, halt_box = _hooks()

    assert hooks.after_tool_call(_ctx("anti_bot_challenge")) is None
    signal = hooks.after_tool_call(_ctx("anti_bot_challenge"))

    assert signal == {"terminate": True}
    assert halt_box[0] is not None
    assert halt_box[0][0] == "anti_bot_challenge"


def test_repeated_route_exhaustion_stops_without_artifacts() -> None:
    hooks, halt_box = _hooks()

    assert hooks.after_tool_call(_ctx("route_already_consumed")) is None
    signal = hooks.after_tool_call(_ctx("route_already_consumed"))

    assert signal == {"terminate": True}
    assert halt_box[0] is not None
    assert halt_box[0][0] == "route_already_consumed"


def test_repeated_validation_misses_become_no_progress() -> None:
    hooks, halt_box = _hooks()

    for _ in range(2):
        assert hooks.after_tool_call(_ctx("target_evidence_not_found")) is None
    signal = hooks.after_tool_call(_ctx("target_evidence_not_found"))

    assert signal == {"terminate": True}
    assert halt_box[0] is not None
    assert halt_box[0][0] == "no_progress"
