from __future__ import annotations

from dataclasses import dataclass, field


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
