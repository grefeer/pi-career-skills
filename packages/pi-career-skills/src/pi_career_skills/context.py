"""Explicit, user-scoped context supplied to a registered Agent tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolContext:
    """The minimum authority a tool receives for one PEV run.

    Tool handlers must use ``user_id`` to enforce every data lookup.  Metadata
    is intentionally a small, already-sanitized context projection rather than
    a database session, raw request, secret or whole user profile.
    """

    user_id: str
    run_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
