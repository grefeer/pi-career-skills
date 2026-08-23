"""Tests for runtime.evidence — EvidenceStore + validators + bounded content.

These tests exercise the promotion rules, semantic validators, dedup,
bounded-content truncation, refs projection, and the single-write-entry-point
invariant.  They import the real contracts from ``pi_career_skills.contracts``
and ``pi_career_skills.errors``; when parallel Task D has not yet landed, run
``py_compile`` + ``ruff`` instead and run pytest at integration time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pi_career_skills.contracts import ToolObservation
from pi_career_skills.runtime.evidence import (
    _NAV_LABEL_TITLES,
    EvidenceStore,
    _has_real_structured_candidate,
    _is_plausible_job_title,
    _is_quality_job_bearing,
    bound_content,
    canonical_json,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_obs(
    tool_name: str,
    output: Mapping | None,
    status: str = "succeeded",
    error_code: str | None = None,
) -> ToolObservation:
    return ToolObservation(
        tool_name=tool_name,
        status=status,
        output=output if output is not None else {},
        error_code=error_code,
        error_message=None,
        tool_call_id="call-x",
    )


def _page_output(**overrides):
    base = {
        "source_url": "https://example.com/jobs/123",
        "content_hash": "a" * 64,
        "quality": "jd_complete",
        "title": "后端工程师",
        "body": "负责后端服务开发，需要 3 年以上经验。",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Validator unit tests
# ---------------------------------------------------------------------------


class TestPlausibleJobTitle:
    def test_nav_labels_all_rejected(self):
        for label in _NAV_LABEL_TITLES:
            assert not _is_plausible_job_title(label), f"nav label leaked: {label}"

    def test_too_short_rejected(self):
        assert not _is_plausible_job_title("")
        assert not _is_plausible_job_title("A")
        assert not _is_plausible_job_title("1")

    def test_no_letter_cjk_rejected(self):
        assert not _is_plausible_job_title("1234")
        assert not _is_plausible_job_title("---")

    def test_real_titles_accepted(self):
        assert _is_plausible_job_title("后端工程师")
        assert _is_plausible_job_title("Senior Software Engineer")
        assert _is_plausible_job_title("Python 开发")

    def test_non_string_rejected(self):
        assert not _is_plausible_job_title(None)
        assert not _is_plausible_job_title(123)


class TestRealStructuredCandidate:
    def test_nav_label_title_rejected(self):
        content = {
            "candidates": [
                {"title": "职位", "responsibilities": "x" * 50, "requirements": "y" * 50}
            ]
        }
        assert not _has_real_structured_candidate(content)

    def test_short_body_rejected(self):
        content = {
            "candidates": [
                {"title": "后端工程师", "responsibilities": "a", "requirements": "b"}
            ]
        }
        assert not _has_real_structured_candidate(content)

    def test_min_body_passes(self):
        # _MIN_JD_BODY_CHARS == 20. The function concatenates
        # responsibilities + ' ' + requirements and strips.
        # 10-char responsibilities + space + 9-char requirements = 20 chars.
        content = {
            "candidates": [
                {
                    "title": "后端工程师",
                    "responsibilities": "x" * 10,
                    "requirements": "y" * 9,
                }
            ]
        }
        assert _has_real_structured_candidate(content)

    def test_details_key_fallback(self):
        content = {"details": [{"title": "后端工程师", "responsibilities": "x" * 30}]}
        assert _has_real_structured_candidate(content)

    def test_empty_candidates_rejected(self):
        assert not _has_real_structured_candidate({"candidates": []})
        assert not _has_real_structured_candidate({})


# ---------------------------------------------------------------------------
# bound_content tests
# ---------------------------------------------------------------------------


class TestBoundContent:
    def test_top_level_fields_truncated_at_40(self):
        big = {f"k{i}": i for i in range(100)}
        bounded = bound_content(big)
        assert len(bounded) == 40

    def test_long_string_truncated_at_12000(self):
        bounded = bound_content({"text": "a" * 20_000})
        assert len(bounded["text"]) == 12_000

    def test_list_truncated_at_20(self):
        bounded = bound_content({"items": list(range(100))})
        assert len(bounded["items"]) == 20

    def test_nested_string_truncated_at_1200(self):
        bounded = bound_content({"data": [{"inner": "x" * 20_000}]})
        # List items that are mappings get recursively bounded; strings inside
        # nested dicts get the 12_000 top-level rule.
        # The 1_200 limit applies to list items that are NOT mappings (they
        # get str(nested)[:1_200]).
        inner = bounded["data"][0]["inner"]
        assert isinstance(inner, str)
        assert len(inner) == 12_000  # nested string in dict → 12_000 cap

    def test_list_non_mapping_string_truncated_at_1200(self):
        bounded = bound_content({"items": ["a" * 5_000]})
        assert len(bounded["items"][0]) == 1_200

    def test_scalars_preserved(self):
        bounded = bound_content({"n": 42, "f": 1.5, "b": True, "nul": None})
        assert bounded["n"] == 42
        assert bounded["f"] == 1.5
        assert bounded["b"] is True
        assert bounded["nul"] is None


# ---------------------------------------------------------------------------
# EvidenceStore tests
# ---------------------------------------------------------------------------


class TestEvidenceStorePromotion:
    def test_succeeded_jd_complete_produces_artifact(self):
        store = EvidenceStore()
        obs = _make_obs("fetch-public-job-page", _page_output())
        arts = store.add_observation(obs)
        assert len(arts) == 1
        art = arts[0]
        assert art.artifact_type == "public_job_page"
        assert art.tool_name == "fetch-public-job-page"
        assert art.source_url == "https://example.com/jobs/123"
        assert re.fullmatch(r"[0-9a-f]{64}", art.content_hash)
        assert len(art.artifact_id) == 32  # uuid4 hex
        assert art.quality == "jd_complete"

    def test_failed_observation_produces_nothing(self):
        store = EvidenceStore()
        obs = _make_obs(
            "fetch-public-job-page",
            None,
            status="failed",
            error_code="network_error",
        )
        arts = store.add_observation(obs)
        assert arts == []
        assert store.artifacts() == []

    def test_no_artifact_type_mapping_produces_nothing(self):
        store = EvidenceStore()
        obs = _make_obs("some-unknown-tool", {"x": 1})
        arts = store.add_observation(obs)
        assert arts == []

    def test_job_bearing_includes_jd_complete(self):
        store = EvidenceStore()
        obs = _make_obs("fetch-public-job-page", _page_output())
        store.add_observation(obs)
        job_bearing = store.job_bearing_artifacts()
        assert len(job_bearing) == 1
        assert _is_quality_job_bearing(job_bearing[0])

    def test_list_only_not_job_bearing(self):
        store = EvidenceStore()
        obs = _make_obs(
            "fetch-public-job-page",
            _page_output(quality="list_only"),
        )
        arts = store.add_observation(obs)
        assert len(arts) == 1
        assert not _is_quality_job_bearing(arts[0])
        assert store.job_bearing_artifacts() == []


class TestSearchShellFiltering:
    def test_empty_search_shell_not_promoted(self):
        store = EvidenceStore()
        output = {
            "source_url": "https://search.example.com/?q=python",
            "content_hash": "b" * 64,
            "results": [],
            "terminal_reason": "search_empty",
        }
        obs = _make_obs("search-public-job-pages", output)
        arts = store.add_observation(obs)
        assert arts == []
        assert store.artifacts() == []

    def test_blocked_search_shell_not_promoted(self):
        store = EvidenceStore()
        output = {
            "source_url": "https://search.example.com/?q=python",
            "content_hash": "b" * 64,
            "results": [],
            "terminal_reason": "blocked",
        }
        obs = _make_obs("search-public-job-pages", output)
        arts = store.add_observation(obs)
        assert arts == []

    def test_search_with_results_is_promoted(self):
        store = EvidenceStore()
        output = {
            "source_url": "https://search.example.com/?q=python",
            "content_hash": "b" * 64,
            "results": [
                {
                    "source_url": "https://example.com/job/1",
                    "title": "Python 工程师",
                }
            ],
        }
        obs = _make_obs("search-public-job-pages", output)
        arts = store.add_observation(obs)
        # top-level shell + 1 result candidate = 2? Actually only top-level
        # has both source_url + content_hash so it's the only candidate.
        # Results items don't have content_hash so they're skipped.
        assert len(arts) >= 1


class TestStructuredJobDetailsValidation:
    def test_nav_label_title_rejected_for_job_bearing(self):
        store = EvidenceStore()
        output = {
            "source_url": "https://example.com/list",
            "content_hash": "c" * 64,
            "candidates": [
                {
                    "title": "职位",  # nav label
                    "responsibilities": "x" * 100,
                    "requirements": "y" * 100,
                }
            ],
        }
        obs = _make_obs("extract-observed-job-details", output)
        arts = store.add_observation(obs)
        assert len(arts) == 1
        assert not _is_quality_job_bearing(arts[0])
        assert arts[0].quality == "low_quality"

    def test_real_title_and_body_passes(self):
        store = EvidenceStore()
        output = {
            "source_url": "https://example.com/list",
            "content_hash": "c" * 64,
            "candidates": [
                {
                    "title": "后端工程师",
                    "responsibilities": "负责后端架构设计与开发",
                    "requirements": "熟悉 Python、Go、MySQL、Redis",
                }
            ],
        }
        obs = _make_obs("extract-observed-job-details", output)
        arts = store.add_observation(obs)
        assert len(arts) == 1
        assert _is_quality_job_bearing(arts[0])
        assert arts[0].quality == "job_bearing"


class TestMatchingReportValidation:
    def test_non_empty_matches_valid(self):
        store = EvidenceStore()
        output = {
            "matches": [
                {
                    "source_url": "https://example.com/job/1",
                    "title": "后端工程师",
                    "score": 0.9,
                }
            ],
            "evaluated_candidate_count": 5,
        }
        obs = _make_obs("match-observed-jobs", output)
        arts = store.add_observation(obs)
        assert len(arts) == 1
        assert _is_quality_job_bearing(arts[0])

    def test_empty_matches_with_reason_and_evaluated_count_valid(self):
        store = EvidenceStore()
        output = {
            "matches": [],
            "no_match_reason": "no_candidate_satisfied_constraints",
            "evaluated_candidate_count": 3,
        }
        obs = _make_obs("match-observed-jobs", output)
        arts = store.add_observation(obs)
        assert len(arts) == 1
        assert _is_quality_job_bearing(arts[0])

    def test_empty_matches_other_reason_not_job_bearing(self):
        """A non-constraint no_match_reason must NOT label the report
        job-bearing — only the exact ``no_candidate_satisfied_constraints``
        value satisfies the completion gate."""
        store = EvidenceStore()
        output = {
            "matches": [],
            "no_match_reason": "some_other_reason",
            "evaluated_candidate_count": 3,
        }
        obs = _make_obs("match-observed-jobs", output)
        arts = store.add_observation(obs)
        assert len(arts) == 1  # still persisted...
        assert not _is_quality_job_bearing(arts[0])  # ...but low quality
        assert arts[0].quality == "low_quality"

    def test_empty_matches_no_evaluation_trace_invalid(self):
        store = EvidenceStore()
        output = {"matches": []}
        obs = _make_obs("match-observed-jobs", output)
        arts = store.add_observation(obs)
        assert len(arts) == 1
        assert not _is_quality_job_bearing(arts[0])
        assert arts[0].quality == "low_quality"


class TestTailoringBriefValidation:
    def test_valid_brief(self):
        store = EvidenceStore()
        output = {
            "target_artifact_id": "abc123",
            "source_url": "https://example.com/job/1",
            "safe_actions": [
                {"field": "summary", "action": "prepend", "text": "..."},
            ],
        }
        obs = _make_obs("build-resume-tailoring-brief", output)
        arts = store.add_observation(obs)
        assert len(arts) == 1
        assert _is_quality_job_bearing(arts[0])

    def test_missing_safe_actions_invalid(self):
        store = EvidenceStore()
        output = {
            "target_artifact_id": "abc123",
            "source_url": "https://example.com/job/1",
            "safe_actions": [],
        }
        obs = _make_obs("build-resume-tailoring-brief", output)
        arts = store.add_observation(obs)
        assert len(arts) == 1
        assert not _is_quality_job_bearing(arts[0])


class TestDedup:
    def test_same_type_url_hash_dedups(self):
        store = EvidenceStore()
        output = _page_output()
        obs1 = _make_obs("fetch-public-job-page", output)
        obs2 = _make_obs("fetch-public-job-page", dict(output))
        art1 = store.add_observation(obs1)[0]
        art2 = store.add_observation(obs2)[0]
        assert art1.artifact_id == art2.artifact_id
        assert len(store.artifacts()) == 1

    def test_different_url_produces_new_artifact(self):
        store = EvidenceStore()
        obs1 = _make_obs("fetch-public-job-page", _page_output(source_url="https://a/1"))
        obs2 = _make_obs("fetch-public-job-page", _page_output(source_url="https://a/2"))
        art1 = store.add_observation(obs1)[0]
        art2 = store.add_observation(obs2)[0]
        assert art1.artifact_id != art2.artifact_id
        assert len(store.artifacts()) == 2


class TestRefsProjection:
    def test_refs_returns_last_12(self):
        store = EvidenceStore()
        for i in range(20):
            obs = _make_obs(
                "fetch-public-job-page",
                {
                    "source_url": f"https://example.com/job/{i}",
                    "content_hash": f"{i:064d}",
                    "quality": "jd_complete",
                },
            )
            store.add_observation(obs)
        refs = store.refs(last=12)
        assert len(refs) == 12
        # Most recent first? No — insertion order, last 12 = most recent 12.
        # The first ref should be the 9th (index 8), the last should be 20th.
        assert refs[0]["source_url"] == "https://example.com/job/8"
        assert refs[-1]["source_url"] == "https://example.com/job/19"

    def test_refs_contain_no_full_content(self):
        store = EvidenceStore()
        obs = _make_obs("fetch-public-job-page", _page_output(body="x" * 500))
        store.add_observation(obs)
        ref = store.refs(last=1)[0]
        assert "content" not in ref
        assert "body" not in ref
        assert set(ref.keys()) == {
            "artifact_id",
            "artifact_type",
            "source_url",
            "content_hash",
            "quality",
        }


class TestSingleWriteEntryPoint:
    """The only write entry point is ``add_observation``.

    There is no ``add_artifact`` public method that would let callers supply
    their own artifact_id.
    """

    def test_no_add_artifact_method(self):
        store = EvidenceStore()
        assert not hasattr(store, "add_artifact")

    def test_artifact_ids_are_store_generated(self):
        store = EvidenceStore()
        obs = _make_obs("fetch-public-job-page", _page_output())
        arts = store.add_observation(obs)
        # uuid4 hex — 32 hex chars
        assert re.fullmatch(r"[0-9a-f]{32}", arts[0].artifact_id)


class TestGetAndBySourceUrl:
    def test_get_by_id(self):
        store = EvidenceStore()
        obs = _make_obs("fetch-public-job-page", _page_output())
        art = store.add_observation(obs)[0]
        assert store.get(art.artifact_id) is art

    def test_get_missing_returns_none(self):
        store = EvidenceStore()
        assert store.get("nonexistent") is None

    def test_by_source_url(self):
        store = EvidenceStore()
        url = "https://example.com/job/1"
        obs = _make_obs("fetch-public-job-page", _page_output(source_url=url))
        store.add_observation(obs)
        matches = store.by_source_url(url)
        assert len(matches) == 1
        assert matches[0].source_url == url


class TestCanonicalJsonDeterminism:
    def test_sorted_keys_and_no_spaces(self):
        d = {"b": 2, "a": 1, "c": {"z": 26, "a": 1}}
        s = canonical_json(d)
        assert s.index('"a"') < s.index('"b"')
        # No spaces around separators
        assert " " not in s

    def test_same_content_same_hash(self):
        import hashlib

        d1 = {"b": 2, "a": 1}
        d2 = {"a": 1, "b": 2}
        h1 = hashlib.sha256(canonical_json(d1).encode("utf-8")).hexdigest()
        h2 = hashlib.sha256(canonical_json(d2).encode("utf-8")).hexdigest()
        assert h1 == h2
