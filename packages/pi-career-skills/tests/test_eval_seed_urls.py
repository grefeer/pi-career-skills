"""Tests for evaluation.seed_urls — source-parity seed URL map.

SEED_URLS is a VERBATIM port of source parity_support.py (17 Q + 14 C-L1
= 31 entries).  ``resolve_seed_urls`` never raises — it returns
``([], "no seeds")`` for unknown ids, so every manifest id resolves
without KeyError.

Covers:
- ALL_SKILLS preserves the source skills plus the intentional local
  career-planning extension.
- Every URL in SEED_URLS starts with http(s):// and has no userinfo.
- resolve_seed_urls returns empty for unknown ids (no KeyError).
- All 83 manifest ids + all chain link ids resolve without KeyError
  (i.e. resolve_seed_urls never raises).
- The set of chain link ids PRESENT in SEED_URLS is exactly C###-L1
  for C001–C015 minus C005-L1; the absent chain-link set is exactly
  C005-L1 plus all ≥L2 links.
- Seed URL keys match the source byte-for-byte (count + specific keys).
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from pi_career_skills.evaluation.seed_urls import (
    ALL_SKILLS,
    SEED_URLS,
    resolve_seed_urls,
)

SOURCE_ROOT = Path(
    r"d:\Program Files\JetBrains\PyCharm Community Edition 2024.2.2"
    r"\proj\langgraph-multi-agent-career-assistant-main"
)
MANIFEST_PATH = SOURCE_ROOT / "tests" / "question" / "redesign" / "manifest.json"

manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


# ====================================================================
# ALL_SKILLS
# ====================================================================


def test_all_skills_includes_career_planning_local_extension() -> None:
    """The migrated planning capability is routable even though source seeds need none."""
    assert ALL_SKILLS == [
        "job-discovery",
        "job-matching",
        "resume-tailoring",
        "career-planning",
    ]


# ====================================================================
# SEED_URLS verbatim count + key set
# ====================================================================


_SOURCE_Q_KEYS = {
    "Q011", "Q013", "Q017", "Q028", "Q034", "Q040", "Q045", "Q055", "Q057",
    "Q081", "Q113", "Q114", "Q115", "Q133", "Q134", "Q143", "Q148",
}

_SOURCE_CHAIN_L1_KEYS = {
    "C001-L1", "C002-L1", "C003-L1", "C004-L1", "C006-L1", "C007-L1",
    "C008-L1", "C009-L1", "C010-L1", "C011-L1", "C012-L1", "C013-L1",
    "C014-L1", "C015-L1",
}


def test_seed_urls_key_set_matches_source() -> None:
    """VERBATIM check: SEED_URLS keys must equal the source mapping keys."""
    expected = _SOURCE_Q_KEYS | _SOURCE_CHAIN_L1_KEYS
    assert set(SEED_URLS.keys()) == expected
    assert len(SEED_URLS) == 31  # 17 Q + 14 chain-L1


def test_no_r_keys_in_seed_urls() -> None:
    """Source parity_support.py has no R### entries."""
    r_keys = {k for k in SEED_URLS if k.startswith("R")}
    assert r_keys == set()


# ====================================================================
# All manifest ids resolve without KeyError (via resolve_seed_urls)
# ====================================================================


def _all_manifest_ids() -> list[str]:
    return [entry["id"] for entry in manifest]


def _all_chain_link_ids() -> list[str]:
    ids: list[str] = []
    for entry in manifest:
        if entry["kind"] != "chain":
            continue
        for n in range(1, entry["links"] + 1):
            ids.append(f"{entry['id']}-L{n}")
    return ids


def test_every_manifest_id_resolves_no_keyerror() -> None:
    """All 83 manifest ids resolve through resolve_seed_urls without error."""
    for mid in _all_manifest_ids():
        urls, note = resolve_seed_urls(mid)
        assert isinstance(urls, list)
        assert isinstance(note, str)


def test_every_chain_link_id_resolves_no_keyerror() -> None:
    """Every chain link id resolves without KeyError."""
    for lid in _all_chain_link_ids():
        urls, note = resolve_seed_urls(lid)
        assert isinstance(urls, list)
        assert isinstance(note, str)


# ====================================================================
# Chain link present/absent set
# ====================================================================


def test_absent_chain_links_are_c005_l1_plus_l2_plus() -> None:
    """Absent chain-link ids = {C005-L1} ∪ {all ≥L2 links}."""
    all_chain_links = set(_all_chain_link_ids())
    present = {lid for lid in all_chain_links if lid in SEED_URLS}
    absent = all_chain_links - present

    # Expected present: C###-L1 for C001–C015 minus C005-L1
    expected_present = _SOURCE_CHAIN_L1_KEYS
    assert present == expected_present, f"Unexpected present: {present - expected_present}, missing: {expected_present - present}"

    # Expected absent: C005-L1 + all L2+
    l2_plus = {lid for lid in all_chain_links if not lid.endswith("-L1")}
    expected_absent = l2_plus | {"C005-L1"}
    assert absent == expected_absent, (
        f"Unexpected absent: {absent - expected_absent}, "
        f"unexpected present: {expected_absent - absent}"
    )


# ====================================================================
# URL format validation — every URL in the map
# ====================================================================


def _all_seed_urls() -> list[str]:
    urls: list[str] = []
    for _qid, (url_list, _note) in SEED_URLS.items():
        urls.extend(url_list)
    return urls


def test_all_urls_use_http_or_https() -> None:
    for url in _all_seed_urls():
        assert url.startswith("http://") or url.startswith("https://"), (
            f"URL does not start with http(s)://: {url}"
        )


def test_no_urls_contain_userinfo() -> None:
    for url in _all_seed_urls():
        parsed = urlparse(url)
        assert parsed.username is None, f"URL has username: {url}"
        assert parsed.password is None, f"URL has password: {url}"


def test_all_urls_have_netloc() -> None:
    for url in _all_seed_urls():
        parsed = urlparse(url)
        assert parsed.netloc, f"URL has no netloc: {url}"


# ====================================================================
# resolve_seed_urls function
# ====================================================================


def test_resolve_seed_urls_known_id() -> None:
    urls, note = resolve_seed_urls("Q011")
    assert isinstance(urls, list)
    assert len(urls) > 0
    assert isinstance(note, str)
    assert len(note) > 0
    assert (urls, note) == SEED_URLS["Q011"]


def test_resolve_seed_urls_unknown_id_returns_empty() -> None:
    urls, note = resolve_seed_urls("NONEXISTENT_XXX")
    assert urls == []
    assert note == "no seeds"


def test_resolve_seed_urls_r_id_returns_empty() -> None:
    """R### ids are not in the source mapping — returns empty."""
    urls, note = resolve_seed_urls("R001")
    assert urls == []
    assert note == "no seeds"


def test_resolve_seed_urls_chain_l1() -> None:
    urls, note = resolve_seed_urls("C001-L1")
    assert len(urls) > 0
    assert isinstance(note, str)


# ====================================================================
# C005-L1 — explicitly absent per source comment
# ====================================================================


def test_c005_l1_absent_from_seed_urls() -> None:
    """Source: 'C005-L1 (台账 3 天): no seeds -- smartsheet first'."""
    assert "C005-L1" not in SEED_URLS
    urls, note = resolve_seed_urls("C005-L1")
    assert urls == []
    assert note == "no seeds"


# ====================================================================
# Notes are non-empty strings
# ====================================================================


def test_all_entries_have_nonempty_note() -> None:
    for qid, (_urls, note) in SEED_URLS.items():
        assert isinstance(note, str) and note, f"{qid} has empty note"
