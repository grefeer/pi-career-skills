"""Tests for runtime state machine and bounded event logger."""

from __future__ import annotations

import json

import pytest

from pi_career_skills.contracts import RunEvent
from pi_career_skills.errors import CareerToolError
from pi_career_skills.runtime.events import EventLogger
from pi_career_skills.runtime.state import RunState, RunStatus, mark_terminal, transition


class TestRunStateTransition:
    def _make_state(self) -> RunState:
        return RunState(
            run_id="run-1",
            attempt_id="att-1",
            synthetic_user_id="user-x",
        )

    def test_initial_status_is_running(self):
        state = self._make_state()
        assert state.status == RunStatus.running
        assert state.terminal is False

    def test_normal_transition_to_succeeded(self):
        state = self._make_state()
        transition(state, RunStatus.succeeded, summary="done")
        assert state.status == RunStatus.succeeded
        assert state.summary == "done"
        assert state.terminal is True

    def test_terminal_rejects_further_transition(self):
        state = self._make_state()
        transition(state, RunStatus.succeeded)
        with pytest.raises(CareerToolError) as exc_info:
            transition(state, RunStatus.failed)
        assert exc_info.value.code == "contract_or_policy_error"

    def test_failed_is_terminal(self):
        state = self._make_state()
        transition(state, RunStatus.failed, error_code="x", error_message="y")
        assert state.terminal is True
        assert state.error_code == "x"
        assert state.error_message == "y"

    def test_cancelled_is_terminal(self):
        state = self._make_state()
        transition(state, RunStatus.cancelled)
        assert state.terminal is True

    def test_waiting_user_not_terminal(self):
        state = self._make_state()
        transition(state, RunStatus.waiting_user)
        assert state.terminal is False
        # Can resume back to running
        transition(state, RunStatus.running)
        assert state.status == RunStatus.running

    def test_mark_terminal_explicit(self):
        state = self._make_state()
        mark_terminal(state)
        with pytest.raises(CareerToolError):
            transition(state, RunStatus.succeeded)

    def test_error_code_and_message_only_when_provided(self):
        state = self._make_state()
        state.error_code = "old_code"
        state.error_message = "old_msg"
        transition(state, RunStatus.waiting_user, summary="paused")
        assert state.error_code == "old_code"  # unchanged
        assert state.error_message == "old_msg"
        assert state.summary == "paused"


class TestEventLogger:
    def test_sequential_seq_numbers(self):
        log = EventLogger()
        s1 = log.append("step_started", {})
        s2 = log.append("tool_called", {})
        s3 = log.append("step_finished", {})
        assert s1 == 1
        assert s2 == 2
        assert s3 == 3
        events = log.events()
        assert [e.seq for e in events] == [1, 2, 3]
        assert events[0].type == "step_started"

    def test_small_payload_preserved(self):
        log = EventLogger(max_payload_bytes=4096)
        payload = {"key": "value", "n": 42}
        log.append("test", payload)
        assert log.events()[0].payload == payload

    def test_oversize_payload_truncated(self):
        log = EventLogger(max_payload_bytes=32)
        big_payload = {"text": "x" * 10_000}
        log.append("big", big_payload)
        event = log.events()[0]
        assert event.payload == {
            "_payload_truncated": True,
            "original_bytes": pytest.approx(10_000, abs=50),
        }
        # Verify it really is small
        serialized = json.dumps(event.payload)
        assert len(serialized.encode("utf-8")) < 200

    def test_events_returns_snapshot(self):
        log = EventLogger()
        log.append("a", {})
        snap = log.events()
        log.append("b", {})
        assert len(snap) == 1
        assert len(log.events()) == 2

    def test_events_carry_constructor_run_and_attempt_id(self):
        log = EventLogger(run_id="r1", attempt_id="a1")
        log.append("step_started", {})
        event = log.events()[0]
        assert event.run_id == "r1"
        assert event.attempt_id == "a1"

    def test_events_default_run_attempt_ids(self):
        log = EventLogger()
        log.append("step_started", {})
        event = log.events()[0]
        assert event.run_id == ""
        assert event.attempt_id is None

    def test_events_are_contracts_run_event(self):
        """The emitted record IS pi_career_skills.contracts.RunEvent — the
        single canonical event type; the local shadowing variant is gone."""
        log = EventLogger(run_id="r1")
        log.append("step_started", {"k": "v"})
        event = log.events()[0]
        assert isinstance(event, RunEvent)
        assert event.payload == {"k": "v"}
        # Same type identity as the package-level export.
        from pi_career_skills.runtime import RunEvent as RuntimeRunEvent

        assert RuntimeRunEvent is RunEvent
