"""Two-level job taxonomy classification (FindJobs TaxonomyManager port, B2).

A deterministic, LLM-free keyword-scoring classifier over the reviewed
``data/job_taxonomy.json`` tree (docs/findjobs-optimization-plan.zh-CN.md
§5.2): each level-2 entry carries keywords; ``classify_text`` counts
keyword hits in a JD text (English case-insensitive with word boundaries,
Chinese literal), picks the highest-scoring level-2 entry (first-in-file
wins ties, so the result is stable across calls), and returns
``(level1, level2)`` - or ``("", "")`` for no hit.

The tree is seeded from the archived ``_ROLE_FAMILY_MARKERS`` families
(product / dev / design / algo / data / ops / test / security / research,
git ea0a70b) and extended to 16 level-1 categories; the archived code
itself is not touched.  The file is human-reviewed (reviewed_at / reviewer
recorded in the plan appendix); the runtime never calls an LLM.

No network, no DB: pure deterministic keyword matching.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files as pkg_files

_RES = pkg_files("pi_career_skills") / "resources" / "data"
_TAXONOMY_PATH = _RES / "job_taxonomy.json"


@dataclass(frozen=True)
class TaxonomyEntry:
    """One level-2 role with its matching keywords."""

    level1: str
    level2: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class TaxonomyIndex:
    """Loaded and validated taxonomy tree (file order preserved)."""

    level1: tuple[str, ...]
    entries: tuple[TaxonomyEntry, ...]


@lru_cache(maxsize=1)
def load_taxonomy() -> TaxonomyIndex:
    """Load and validate the reviewed taxonomy tree (cached, never LLM-built)."""
    raw = json.loads(_TAXONOMY_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("job_taxonomy.json must be a JSON object")
    level1 = raw.get("level1")
    if not isinstance(level1, list) or not level1:
        raise ValueError("job_taxonomy.json must contain a non-empty 'level1' list")
    names: list[str] = []
    entries: list[TaxonomyEntry] = []
    for category in level1:
        if not isinstance(category, dict):
            raise ValueError("each level1 category must be an object")
        name = category.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("each level1 category needs a non-empty 'name'")
        names.append(name)
        level2 = category.get("level2")
        if not isinstance(level2, list) or not level2:
            raise ValueError(f"level1 '{name}' needs a non-empty 'level2' list")
        for entry in level2:
            if not isinstance(entry, dict):
                raise ValueError("each level2 entry must be an object")
            entry_name = entry.get("name")
            keywords = entry.get("keywords")
            if not isinstance(entry_name, str) or not entry_name:
                raise ValueError(
                    f"level2 entry under '{name}' needs a non-empty 'name'"
                )
            if (
                not isinstance(keywords, list)
                or not keywords
                or not all(isinstance(kw, str) and kw for kw in keywords)
            ):
                raise ValueError(
                    f"level2 '{entry_name}' needs a non-empty list of keyword strings"
                )
            entries.append(
                TaxonomyEntry(
                    level1=name, level2=entry_name, keywords=tuple(keywords)
                )
            )
    return TaxonomyIndex(level1=tuple(names), entries=tuple(entries))


def _keyword_pattern(keyword: str) -> re.Pattern:
    """English keywords match case-insensitively with word boundaries (so a
    single-letter ``cv`` never fires inside "cover"); Chinese is literal."""
    if keyword.isascii() and keyword.replace(" ", "").isalnum():
        return re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE)
    return re.compile(re.escape(keyword))


@lru_cache(maxsize=1)
def _keyword_patterns() -> dict[str, re.Pattern]:
    """Compile every keyword exactly once (per keyword string)."""
    patterns: dict[str, re.Pattern] = {}
    for entry in load_taxonomy().entries:
        for keyword in entry.keywords:
            patterns[keyword] = _keyword_pattern(keyword)
    return patterns


def classify_text(text: str) -> tuple[str, str]:
    """(level1, level2) of the best-matching entry; ("", "") when no keyword
    hits.  Deterministic: hits are counted per keyword (not per occurrence,
    so a repeated word never dominates), ties go to the first entry in file
    order."""
    if not text:
        return "", ""
    patterns = _keyword_patterns()
    best = ("", "")
    best_hits = 0
    for entry in load_taxonomy().entries:
        hits = sum(
            1 for keyword in entry.keywords if patterns[keyword].search(text)
        )
        if hits > best_hits:
            best_hits = hits
            best = (entry.level1, entry.level2)
    return best


def taxonomy_tags(text: str) -> list[str]:
    """Enrichment form of ``classify_text``: [level1, level2], or [] when
    unclassified (the NormalizedJobCandidate.taxonomy default)."""
    level1, level2 = classify_text(text)
    if not level1:
        return []
    return [level1, level2]
