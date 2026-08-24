"""Profile → confirmed facts — VERBATIM port of source extract_evidence_candidates.

This module implements the same evidence-candidate extraction and profile-fact
assembly used in the source project's ``tests/question/parity_support.py`` and
``backend/app/services/profile_parser.py``.  The algorithm is mirrored
byte-for-byte so evaluation records produced here are comparable with the
source baseline.

Pure, deterministic, no side effects.
"""

from __future__ import annotations

import re
from collections import namedtuple

# Mirror of backend.app.domain.profiles.EvidenceCandidate
EvidenceCandidate = namedtuple(
    "EvidenceCandidate",
    ["field_path", "candidate_value", "evidence_excerpt", "confidence"],
)

_SECTION_ALIASES: dict[str, str] = {
    "教育经历": "education",
    "教育背景": "education",
    "实习经历": "experience",
    "工作经历": "experience",
    "项目经历": "projects",
    "技能": "skills",
    "专业技能": "skills",
    "获奖": "awards",
    "荣誉奖项": "awards",
    "证书": "certificates",
    "语言成绩": "languages",
    "作品链接": "portfolio_links",
}


def _extract_evidence_candidates(text: str) -> list[EvidenceCandidate]:
    """Mirror of ``backend.app.services.profile_parser.extract_evidence_candidates``.

    Parses a resume text into structured evidence candidates.  Section
    headings are recognized via ``_SECTION_ALIASES``; basics (name/email/
    phone) are extracted with regex.  Skills are split on common separators
    and deduplicated.
    """
    candidates: list[EvidenceCandidate] = []
    lines = text.splitlines()
    current_section: str | None = None
    section_lines: list[str] = []
    seen_name = False
    seen_email = False
    seen_phone = False
    _email_re = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
    _phone_re = re.compile(r"1[3-9]\d{9}")
    _url_re = re.compile(r"https?://\S+")

    portfolio_urls: list[str] = []

    def _add_basic_candidate(
        field_path: str,
        value: object,
        excerpt: str,
        confidence: int,
    ) -> None:
        candidates.append(
            EvidenceCandidate(
                field_path=field_path,
                candidate_value=value,
                evidence_excerpt=excerpt[:500],
                confidence=confidence,
            )
        )

    def _flush_section() -> None:
        nonlocal current_section, section_lines
        if current_section is None or not section_lines:
            return
        if current_section == "skills":
            items: list[str] = []
            for line in section_lines:
                for sep in ("、", ",", "，", ";", "；", " "):
                    if sep in line:
                        parts = [p.strip() for p in line.split(sep) if p.strip()]
                        items.extend(parts)
                        break
                else:
                    items.append(line.strip())
            deduped = list(dict.fromkeys(items))
            _add_basic_candidate("skills", deduped, " ".join(section_lines), 85)
        else:
            cleaned = [line.strip() for line in section_lines if line.strip()]
            _add_basic_candidate(
                current_section,
                cleaned,
                " ".join(cleaned),
                80,
            )
        section_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        heading_key = stripped.rstrip("：:")
        if heading_key in _SECTION_ALIASES:
            _flush_section()
            current_section = _SECTION_ALIASES[heading_key]
            continue

        if not seen_name and current_section is None and len(stripped) <= 120:
            _add_basic_candidate("basics.name", stripped, stripped, 65)
            seen_name = True
            continue

        email_match = _email_re.search(stripped)
        if not seen_email and email_match:
            _add_basic_candidate(
                "basics.email", email_match.group(0), email_match.group(0), 90
            )
            seen_email = True

        phone_match = _phone_re.search(stripped)
        if not seen_phone and phone_match:
            _add_basic_candidate(
                "basics.phone", phone_match.group(0), phone_match.group(0), 90
            )
            seen_phone = True

        for url in _url_re.findall(stripped):
            if url not in portfolio_urls:
                portfolio_urls.append(url)

        if current_section is not None:
            section_lines.append(stripped)

    _flush_section()

    if portfolio_urls:
        _add_basic_candidate(
            "portfolio_links",
            portfolio_urls,
            " ".join(portfolio_urls),
            80,
        )

    return candidates


def build_profile_facts(profile: dict) -> dict[str, str]:
    """Render a reviewed question profile into structured confirmed facts.

    Verbatim port of ``tests/question/parity_support.py:build_profile_facts``.

    If ``profile["resume_text"]`` is a non-empty string, the facts are
    extracted directly from it.  Otherwise a synthetic resume is assembled
    from ``profile["summary"]`` (regex-matched skills + experience) and
    ``profile["role"]``, then passed through the same extraction pipeline.

    Args:
        profile: A dict with at least ``"role"`` and ``"summary"`` keys,
            and optionally ``"resume_text"``.

    Returns:
        Mapping of ``field_path → candidate_value`` for every evidence
        candidate extracted from the (possibly synthetic) resume text.
    """
    if profile.get("resume_text"):
        return {
            candidate.field_path: candidate.candidate_value
            for candidate in _extract_evidence_candidates(profile["resume_text"])
        }

    summary = profile["summary"]
    skills_match = re.search(r"技能：(.+?)(?:，|$)", summary)
    skills = skills_match.group(1) if skills_match else ""
    exp_match = re.search(r"(应届生（校招）|社招（\d+ 年经验）)", summary)
    experience = exp_match.group(1) if exp_match else ""
    resume_text = "\n".join(
        line
        for line in (f"{profile['role']}（评测画像）", "教育经历", experience, "技能", skills)
        if line
    )
    return {
        candidate.field_path: candidate.candidate_value
        for candidate in _extract_evidence_candidates(resume_text)
    }


__all__ = [
    "EvidenceCandidate",
    "build_profile_facts",
]
