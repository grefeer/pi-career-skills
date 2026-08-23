"""Bounded in-memory event stream for a single run.

Events carry a monotonically increasing sequence number and a bounded payload.
Oversize payloads are replaced with a truncation stub so a runaway observation
cannot blow up the event stream.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass


@dataclass
class RunEvent:
    """A single run event with an opaque but bounded payload."""

    seq: int
    type: str
    payload: dict

    def as_dict(self) -> dict:
        return {"seq": self.seq, "type": self.type, "payload": self.payload}


class EventLogger:
    """Append-only bounded event log.

    Thread-safe: writers and readers may run on different threads
    (e.g. controller vs. to_thread handler).
    """

    def __init__(self, max_payload_bytes: int = 4096) -> None:
        self._max_payload_bytes = max_payload_bytes
        self._events: list[RunEvent] = []
        self._seq_counter = 0
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[RunEvent], None]] = []

    def append(self, event_type: str, payload: dict) -> int:
        """Append a new event and return its sequence number.

        Oversize payloads are replaced with a truncation stub carrying the
        original size so the event stream stays bounded.
        """
        bounded = self._bounded_payload(payload)
        with self._lock:
            self._seq_counter += 1
            seq = self._seq_counter
            event = RunEvent(seq=seq, type=event_type, payload=bounded)
            self._events.append(event)
        for subscriber in list(self._subscribers):
            with suppress(Exception):
                # Subscriber exceptions never break the log.
                subscriber(event)
        return seq

    def events(self) -> list[RunEvent]:
        """Return a snapshot of all events in order."""
        with self._lock:
            return list(self._events)

    def subscribe(self, callback: Callable[[RunEvent], None]) -> None:
        """Register a callback invoked on every new append."""
        self._subscribers.append(callback)

    def _bounded_payload(self, payload: dict) -> dict:
        try:
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return {"_payload_truncated": True, "original_bytes": -1}
        if len(serialized.encode("utf-8")) <= self._max_payload_bytes:
            return payload
        return {"_payload_truncated": True, "original_bytes": len(serialized)}


__all__ = ["RunEvent", "EventLogger"]
