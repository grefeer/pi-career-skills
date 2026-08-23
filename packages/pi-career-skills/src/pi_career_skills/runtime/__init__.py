"""Run-level harness primitives (state, events, evidence).

These modules implement the trusted-kernel in-memory equivalents of the
DeepAgents backend's kernel evidence sink, run state machine, and bounded
event logger — ported for the pi-py parity runtime.
"""

from ..contracts import RunEvent
from . import evidence  # noqa: F401  (re-export via aliases below)
from .events import EventLogger
from .state import RunState, RunStatus, mark_terminal, transition

# Re-exported from evidence module
EvidenceStore = evidence.EvidenceStore
TOOL_ARTIFACT_TYPE = evidence.TOOL_ARTIFACT_TYPE
bound_content = evidence.bound_content
canonical_json = evidence.canonical_json
content_hash_of = evidence.content_hash_of
_NAV_LABEL_TITLES = evidence._NAV_LABEL_TITLES
_MIN_JD_BODY_CHARS = evidence._MIN_JD_BODY_CHARS
_is_plausible_job_title = evidence._is_plausible_job_title
_has_real_structured_candidate = evidence._has_real_structured_candidate
_is_quality_job_bearing = evidence._is_quality_job_bearing

__all__ = [
    "EventLogger",
    "RunEvent",
    "EvidenceStore",
    "RunState",
    "RunStatus",
    "mark_terminal",
    "transition",
    "bound_content",
    "canonical_json",
    "content_hash_of",
    "TOOL_ARTIFACT_TYPE",
    "_NAV_LABEL_TITLES",
    "_MIN_JD_BODY_CHARS",
    "_is_plausible_job_title",
    "_has_real_structured_candidate",
    "_is_quality_job_bearing",
]
