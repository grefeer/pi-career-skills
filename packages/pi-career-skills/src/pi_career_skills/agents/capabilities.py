"""Structured capability metadata for supervisor and skill agents."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

TASK_FIELDS = frozenset(
    {"objective", "input_refs", "constraints", "expected_output"}
)


@dataclass(frozen=True)
class CapabilityDefinition:
    """One routable skill capability and its trusted-kernel contract."""

    name: str
    description: str
    accepts: frozenset[str]
    returns: frozenset[str]
    side_effects: frozenset[str]
    tool_names: tuple[str, ...]
    prerequisite: str
    completion_key: str
    default_budget: Mapping[str, int]
    model_key: str
    prompt_resource: str


class CapabilityRegistry(Mapping[str, CapabilityDefinition]):
    def __init__(self, definitions: Mapping[str, CapabilityDefinition]) -> None:
        self._definitions = dict(definitions)

    def __getitem__(self, name: str) -> CapabilityDefinition:
        return self._definitions[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._definitions)

    def __len__(self) -> int:
        return len(self._definitions)

    def get(self, name: str, default: Any = None) -> CapabilityDefinition | Any:
        return self._definitions.get(name, default)

    def require(self, name: str) -> CapabilityDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise ValueError(f"unknown career capability: {name}") from exc


def _definition(
    *,
    name: str,
    description: str,
    returns: set[str],
    side_effects: set[str],
    prerequisite: str,
    completion_key: str,
    default_budget: dict[str, int],
    tool_names: list[str],
) -> CapabilityDefinition:
    return CapabilityDefinition(
        name=name,
        description=description,
        accepts=TASK_FIELDS,
        returns=frozenset(returns),
        side_effects=frozenset(side_effects),
        tool_names=tuple(tool_names),
        prerequisite=prerequisite,
        completion_key=completion_key,
        default_budget=default_budget,
        model_key=name,
        prompt_resource=f"archived_skills/{name}/SKILL.md",
    )


def build_capability_registry(
    tool_catalog: Mapping[str, list[str]] | None = None,
) -> CapabilityRegistry:
    """Build capability metadata from the registered per-skill tool catalog."""

    if tool_catalog is None:
        from ..registry import TOOL_CATALOG_BY_SKILL

        tool_catalog = TOOL_CATALOG_BY_SKILL

    definitions = {
        "job-discovery": _definition(
            name="job-discovery",
            description="收集公开职位证据并提取结构化 JD",
            returns={"public_job_page", "structured_job_details", "job_search_results"},
            side_effects={"read_public_evidence", "write_run_artifact"},
            prerequisite="none",
            completion_key="job-discovery",
            default_budget={
                "agent_turns": 24,
                "tool_calls": 48,
                "model_requests": 32,
                "input_tokens": 200_000,
                "wall_clock_seconds": 300,
            },
            tool_names=list(tool_catalog.get("job-discovery", [])),
        ),
        "job-matching": _definition(
            name="job-matching",
            description="对已观察职位做透明、可追溯的匹配排序",
            returns={"job_matching_report"},
            side_effects={"write_run_artifact"},
            prerequisite="structured_job_details",
            completion_key="job-matching",
            default_budget={
                "agent_turns": 12,
                "tool_calls": 12,
                "model_requests": 16,
                "input_tokens": 100_000,
                "wall_clock_seconds": 180,
            },
            tool_names=list(tool_catalog.get("job-matching", [])),
        ),
        "resume-tailoring": _definition(
            name="resume-tailoring",
            description="针对目标 JD 生成不可虚构的简历修改建议",
            returns={"resume_tailoring_brief"},
            side_effects={"write_run_artifact"},
            prerequisite="job_bearing_artifact",
            completion_key="resume-tailoring",
            default_budget={
                "agent_turns": 12,
                "tool_calls": 12,
                "model_requests": 16,
                "input_tokens": 100_000,
                "wall_clock_seconds": 180,
            },
            tool_names=list(tool_catalog.get("resume-tailoring", [])),
        ),
        "career-planning": _definition(
            name="career-planning",
            description="基于目标 JD 生成证据可追溯的求职准备计划",
            returns={"career_preparation_plan"},
            side_effects={"write_run_artifact"},
            prerequisite="job_bearing_artifact",
            completion_key="career-planning",
            default_budget={
                "agent_turns": 12,
                "tool_calls": 12,
                "model_requests": 16,
                "input_tokens": 100_000,
                "wall_clock_seconds": 180,
            },
            tool_names=list(tool_catalog.get("career-planning", [])),
        ),
    }
    missing = set(definitions) - set(tool_catalog)
    if missing:
        raise ValueError(f"missing capability tool catalogs: {sorted(missing)}")
    return CapabilityRegistry(definitions)


CAPABILITY_REGISTRY = build_capability_registry()


__all__ = [
    "TASK_FIELDS",
    "CapabilityDefinition",
    "CapabilityRegistry",
    "CAPABILITY_REGISTRY",
    "build_capability_registry",
]
