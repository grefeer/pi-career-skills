"""Job strength signals (FindJobs SIGNAL_RULES port, B1).

A deterministic, LLM-free analysis of how concrete a JD text is: a small
weighted rule table (years / skill stack / degree / numbered duties /
bonus items) fires on regex evidence, the weights sum to a score, the
score maps to a tier (high / medium / low), and every hit keeps its
verbatim evidence text for audit (docs/findjobs-optimization-plan.zh-CN.md
§5.1).  ``base_score`` is the tier's additive hint for downstream
scoring: it is an OPTIONAL input there - the existing
``min(100, matched * 34)`` matching path is never changed.

Weights and tier thresholds are empirical calibrations (plan §9 known
limitations): years + skill stack are strong signals (weight 2), degree /
numbered duty list / bonus items are supporting signals (weight 1);
score >= 5 -> high, >= 3 -> medium, else low.

No LLM call, no network, no DB: pure deterministic regex over the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal

from pi_career_skills.business.common.skill_validator import load_skill_tags

Tier = Literal["high", "medium", "low"]

#: "3年以上相关工作经验" / "五年及以上工作经验" / "2年经历" - the plan's
#: ``(\d+)\s*(年|年以上).*经验`` draft widened to Chinese numerals and
#: "经历".  A trailing-numeral JD ("工作经验3年") is out of scope (limitation).
_YEARS_RE = re.compile(
    r"(?:[\d一二三四五六七八九十百]+)\s*年(?:以上|及以上)?\s*(?:相关)?\s*(?:工作)?(?:经验|经历)"
)

#: Degree whitelist tiers (B3) as a presence signal: the JD states a real
#: degree bar.  "学历不限" carries no bar and is deliberately not a hit.
_DEGREE_RE = re.compile(r"(?:本科|硕士|博士|大专)(?:及以上|以上)?")

#: Bonus items: "加分" / "优先考虑" / "优先录用" (B3 preferred markers).
_BONUS_RE = re.compile(r"(?:加分项?|优先考虑|优先录用)")

#: Numbered duty lines: "1. xxx" / "一、xxx" / "①" shapes, per line.  A JD
#: with >=3 such lines carries an explicit duty list (plan §5.1).
_LIST_ITEM_RE = re.compile(
    r"^\s*(?:[0-9一二三四五六七八九十]+)[、.)．]\s*(.+)", re.MULTILINE
)

#: Max evidence chars kept per numbered duty line (audit excerpt, never a
#: full-field rewrite).
_MAX_EVIDENCE_PER_ITEM = 40

#: Tier thresholds and additive base scores, most-specific first
#: (empirical calibration, plan §9).  Score ceiling: 2+2+1+1+1 = 7; any
#: score below the lowest threshold is the default low tier (no entry).
_TIERS: tuple[tuple[int, Tier, int], ...] = (
    (5, "high", 10),
    (3, "medium", 5),
)


@dataclass(frozen=True)
class JobStrengthSignal:
    """One fired rule with its verbatim evidence text."""

    label: str
    weight: int
    evidence: str


@dataclass(frozen=True)
class JobStrengthResult:
    """Aggregate strength of a JD text."""

    score: int
    tier: Tier
    base_score: int
    signals: list[JobStrengthSignal] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serializable form for artifact persistence / audit."""
        return {
            "score": self.score,
            "tier": self.tier,
            "base_score": self.base_score,
            "evidence": [
                {
                    "label": signal.label,
                    "weight": signal.weight,
                    "evidence": signal.evidence,
                }
                for signal in self.signals
            ],
        }


@lru_cache(maxsize=1)
def _skill_stack_pattern() -> re.Pattern:
    """``熟悉|精通|掌握`` + a closed-set skill (A2), longest tag first so
    ``Agent开发`` wins over ``Agent`` inside the alternation."""
    tags = sorted(load_skill_tags(), key=len, reverse=True)
    alternation = "|".join(re.escape(tag) for tag in tags)
    return re.compile(
        rf"(?:熟悉|精通|掌握|熟练(?:使用|掌握)?)\s*[:：]?\s*(?:{alternation})"
    )


def _tier_for(score: int) -> tuple[Tier, int]:
    """Score -> (tier, base_score); the first threshold the score clears,
    else the low tier with base_score 0 (always reachable for score < 3)."""
    for threshold, candidate_tier, candidate_base in _TIERS:
        if score >= threshold:
            return candidate_tier, candidate_base
    return "low", 0


def analyze_job_strength(text: str) -> JobStrengthResult:
    """Score a JD text's concreteness; never raises, empty text -> low."""
    signals: list[JobStrengthSignal] = []
    for label, weight, pattern in (
        ("明确年限要求", 2, _YEARS_RE),
        ("明确学历", 1, _DEGREE_RE),
        ("明确加分项", 1, _BONUS_RE),
    ):
        match = pattern.search(text)
        if match is not None:
            signals.append(
                JobStrengthSignal(label=label, weight=weight, evidence=match.group(0))
            )
    skill_match = _skill_stack_pattern().search(text)
    if skill_match is not None:
        signals.append(
            JobStrengthSignal(label="明确技能栈", weight=2, evidence=skill_match.group(0))
        )
    duties = _LIST_ITEM_RE.findall(text)
    if len(duties) >= 3:
        excerpt = "；".join(
            part.strip()[:_MAX_EVIDENCE_PER_ITEM] for part in duties[:3]
        )
        signals.append(JobStrengthSignal(label="明确职责清单", weight=1, evidence=excerpt))
    score = sum(signal.weight for signal in signals)
    tier, base_score = _tier_for(score)
    return JobStrengthResult(
        score=score, tier=tier, base_score=base_score, signals=signals
    )
