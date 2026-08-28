"""Closed-set skill tag validation (FindJobs SkillRepository port, A2).

Deterministic and LLM-free post-processing for skill tags emitted by the LLM
JD extractor: a tag must be a member of the reviewed closed set
(``data/skill_tags.json``, <=80 entries) or a curated alias of one; an
unknown tag is dropped or remapped, never invented.  Low-information labels
({AI, 技术, 数学, ...} - FindJobs ``LOW_INFORMATION_SKILLS``) never survive.
A below-minimum result falls back to regex keyword hits from the JD text so
a silent "no skills" is never produced from a text that names them.

No LLM call, no network, no DB: this is a pure deterministic module.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib.resources import files as pkg_files

_RES = pkg_files("pi_career_skills") / "resources" / "data"
_SKILL_TAGS_PATH = _RES / "skill_tags.json"

#: Curated alias table: model-emitted synonyms -> canonical closed-set tag.
#: Remapping is applied before membership checks so aliases never leak.
_SKILL_ALIASES: dict[str, str] = {
    "python": "Python",
    "python3": "Python",
    "c++": "C++",
    "cpp": "C++",
    "c#": "C#",
    "js": "JavaScript",
    "javascript/typescript": "JavaScript",
    "ts": "TypeScript",
    "nlp": "NLP",
    "自然语言处理(nlp)": "自然语言处理",
    "cv": "计算机视觉",
    "llm": "大模型",
    "大语言模型": "大模型",
    "大语言模型(llm)": "大模型",
    "rag": "RAG",
    "检索增强生成": "RAG",
    "agent": "Agent",
    "智能体": "Agent",
    "深度学习框架": "深度学习",
    "机器学习算法": "机器学习",
    "推荐算法": "推荐系统",
    "分布式": "分布式系统",
    "数据库(mysql)": "MySQL",
    "数据结构": "数据结构与算法",
    "算法": "数据结构与算法",
}

#: Low-information labels that match too many JDs to be signal; they are
#: filtered after membership (FindJobs ``LOW_INFORMATION_SKILLS`` semantics).
_LOW_INFORMATION_SKILLS: frozenset[str] = frozenset(
    {
        "AI",
        "人工智能",
        "技术",
        "数学",
        "计算机",
        "科研",
        "能力",
        "技能",
        "开发",
        "工程师",
        "基础",
    }
)

#: Regex fallback source for the below-minimum path: a closed-set tag is kept
#: when its name appears in the JD text (English case-insensitive with word
#: boundaries, Chinese literal).  Boundaries stop single-letter tags like
#: ``C`` or ``Go`` from firing inside unrelated words ("proficient",
#: "together"); Chinese has no word-boundary concept, so the literal match
#: is the whole-word match there.
def _tag_pattern(tag: str) -> re.Pattern:
    if tag.isascii() and tag.replace(" ", "").isalnum():
        return re.compile(rf"\b{re.escape(tag)}\b", re.IGNORECASE)
    return re.compile(re.escape(tag))


@lru_cache(maxsize=1)
def load_skill_tags() -> list[str]:
    """Load and validate the reviewed closed set (cached, never LLM-built)."""
    raw = json.loads(_SKILL_TAGS_PATH.read_text(encoding="utf-8"))
    tags = raw.get("tags") if isinstance(raw, dict) else None
    if (
        not isinstance(tags, list)
        or not tags  # an empty closed set would silently disable validation
        or not all(isinstance(tag, str) and tag for tag in tags)
    ):
        raise ValueError("skill_tags.json must contain a non-empty list of strings under 'tags'")
    if len(tags) > 80:
        raise ValueError("skill closed set exceeds the 80-entry ceiling")
    return list(dict.fromkeys(tags))


def _canonical_key(tag: str) -> str:
    """Membership key: lowercase ASCII (English aliases), verbatim Chinese."""
    return tag.lower() if tag.isascii() else tag


@lru_cache(maxsize=1)
def _closed_map() -> dict[str, str]:
    """Canonical-key -> canonical-tag map for O(1) membership checks."""
    return {_canonical_key(tag): tag for tag in load_skill_tags()}


def normalize_skill(tag: str) -> str | None:
    """Map one raw tag to a closed-set member, or None when not usable.

    Pipeline: strip -> curated alias remap -> membership check (English
    case-insensitive, Chinese literal).  A tag that survives all stages is
    returned in its canonical closed-set spelling.
    """
    raw = tag.strip()
    if not raw:
        return None
    closed = _closed_map()
    for candidate in (_SKILL_ALIASES.get(_canonical_key(raw), raw), raw):
        canonical = _canonical_key(candidate)
        if canonical in closed:
            return closed[canonical]
    return None


def filter_low_information(tags: list[str]) -> list[str]:
    """Drop low-information labels in order; never reorders survivors."""
    return [tag for tag in tags if tag not in _LOW_INFORMATION_SKILLS]


def _fallback_from_text(tags: list[str], fallback_text: str) -> list[str]:
    """Append closed-set tags literally named in the JD text (dedup, in set order)."""
    if not fallback_text:
        return tags
    text = fallback_text.lower()
    for tag in load_skill_tags():
        if tag in tags:
            continue
        if _tag_pattern(tag).search(text):
            tags.append(tag)
    return tags


def skills_from_text(text: str) -> list[str]:
    """Closed-set tags literally named in the text, in closed-set order.

    Deterministic JD-text skill extraction for cross-JD aggregation (C2):
    the same fallback machinery that rescues a below-minimum model output,
    used directly as the only source.  Empty text -> [].
    """
    return _fallback_from_text([], text)
