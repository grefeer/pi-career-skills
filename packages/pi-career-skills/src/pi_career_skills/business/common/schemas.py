from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class NormalizedJobCandidate:
    title: str | None = None
    company_name: str | None = None
    department: str | None = None
    description_text: str = ""
    responsibilities: str = ""
    requirements: str = ""
    locations: list[str] = field(default_factory=list)
    recruitment_types: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    apply_url: str | None = None
    application_channel_json: dict | None = None
    deadline_text: str | None = None
    published_at: str | None = None
    referral_code: str | None = None
    confidence: float = 0.0
    evidence_refs: list[dict] = field(default_factory=list)
    normalization_warnings: list[str] = field(default_factory=list)
    # FindJobs-derived structured features (v1: optional fields, no MySQL
    # migration - see docs/findjobs-optimization-plan.zh-CN.md §6).
    skills: list[str] = field(default_factory=list)   # A2: closed-set skill tags
    min_degree: str | None = None                      # B3: degree whitelist value
    priority: str = "unknown"                          # B3: must/preferred/unknown
    taxonomy: list[str] = field(default_factory=list)  # B2: [level1, level2]
    # B1: strength dict {score, tier, base_score, evidence[]} from
    # tools.job_strength; optional and serialization-friendly by design.
    strength: dict | None = None


@dataclass
class StrategyRecord:
    """In-memory representation of a matched strategy (decoupled from ORM)."""

    id: str
    url_pattern: str
    site_type: str
    description: str = ""
    priority: int = 0
    adapter: str | None = None
    plan_yaml: str = ""
    status: str = "active"
    success_count: int = 0

    @classmethod
    def from_orm(cls, orm_obj: Any) -> StrategyRecord:
        """Build from an ORM-like object exposing the same attributes."""
        return cls(
            id=orm_obj.id,
            url_pattern=orm_obj.url_pattern,
            site_type=orm_obj.site_type,
            description=orm_obj.description or "",
            priority=orm_obj.priority,
            adapter=orm_obj.adapter,
            plan_yaml=orm_obj.plan_yaml,
            status=orm_obj.status,
            success_count=orm_obj.success_count,
        )


RecruitmentType = Literal["campus", "internship", "social"]


@dataclass
class RecruitmentScope:
    """Target recruitment scope for a single discovery task.

    One task targets exactly one ``recruitment_type``. ``social`` has no
    cohort; ``campus``/``internship`` require a ``graduation_year``.
    """

    recruitment_type: RecruitmentType = "campus"
    graduation_year: int | None = 2027

    def __post_init__(self) -> None:
        if self.recruitment_type == "social":
            self.graduation_year = None
            return
        if self.graduation_year is None:
            raise ValueError(
                "graduation_year is required for campus and internship"
            )
