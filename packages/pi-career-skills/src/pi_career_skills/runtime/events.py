"""Bounded in-memory event stream for a single run.

Events carry a monotonically increasing sequence number and a bounded payload.
Oversize payloads are replaced with a truncation stub so a runaway observation
cannot blow up the event stream.

The event record itself is :class:`pi_career_skills.contracts.RunEvent` — the
single canonical event type (a ``BaseModel`` serialized via ``model_dump()``);
no local shadowing variant exists.
"""

from __future__ import annotations

import json
import threading

from ..contracts import RunEvent


class EventLogger:
    """Append-only bounded event log.

    Thread-safe: writers and readers may run on different threads
    (e.g. controller vs. to_thread handler).
    """

    def __init__(
        self,
        max_payload_bytes: int = 4096,
        run_id: str = "",
        attempt_id: str | None = None,
    ) -> None:
        self._max_payload_bytes = max_payload_bytes
        self._run_id = run_id
        self._attempt_id = attempt_id
        self._events: list[RunEvent] = []
        self._seq_counter = 0
        self._lock = threading.Lock()

    def append(self, event_type: str, payload: dict) -> int:
        """Append a new event and return its sequence number.

        Oversize payloads are replaced with a truncation stub carrying the
        original size so the event stream stays bounded.
        """
        bounded = self._bounded_payload(payload)
        with self._lock:
            self._seq_counter += 1
            seq = self._seq_counter
            event = RunEvent(
                seq=seq,
                type=event_type,
                run_id=self._run_id,
                attempt_id=self._attempt_id,
                payload=bounded,
            )
            self._events.append(event)
        return seq

    def events(self) -> list[RunEvent]:
        """Return a snapshot of all events in order."""
        with self._lock:
            return list(self._events)

    def _bounded_payload(self, payload: dict) -> dict:
        try:
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return {"_payload_truncated": True, "original_bytes": -1}
        if len(serialized.encode("utf-8")) <= self._max_payload_bytes:
            return payload
        return {"_payload_truncated": True, "original_bytes": len(serialized)}


__all__ = ["EventLogger", "RunEvent"]
