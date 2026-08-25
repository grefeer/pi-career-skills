"""Direct tests for the common-layer modules (taxonomy, skill_validator,
job_strength, batch_progress) and target_evidence.

Key-point coverage, not exhaustive: verify each module loads, produces the
expected shape, and handles the basic happy + edge paths.
"""

from __future__ import annotations

from pi_career_skills.business.common.batch_progress import (
    BatchResult,
    run_parallel_with_progress,
)
from pi_career_skills.business.common.job_strength import (
    JobStrengthResult,
    analyze_job_strength,
)
from pi_career_skills.business.common.skill_validator import (
    load_skill_tags,
    normalize_skill,
    skills_from_text,
    validate_skills,
)
from pi_career_skills.business.common.taxonomy import (
    classify_text,
    load_taxonomy,
    taxonomy_tags,
)
from pi_career_skills.business.job_discovery.target_evidence import (
    resolve_target_evidence,
)
from pi_career_skills.context import ToolContext

# ---------------------------------------------------------------------------
# ToolContext
# ---------------------------------------------------------------------------


def test_tool_context_defaults() -> None:
    ctx = ToolContext(user_id="u1", run_id="r1")
    assert ctx.user_id == "u1"
    assert ctx.run_id == "r1"
    assert ctx.metadata == {}
    # frozen: cannot mutate
    try:
        ctx.user_id = "x"  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("ToolContext should be frozen")


# ---------------------------------------------------------------------------
# taxonomy
# ---------------------------------------------------------------------------


def test_taxonomy_loads_from_resources() -> None:
    idx = load_taxonomy()
    assert len(idx.level1) > 0
    assert len(idx.entries) > len(idx.level1)
    # Level-1 names must be strings; entries must reference valid level-1 names.
    for entry in idx.entries:
        assert entry.level1 in idx.level1
        assert len(entry.keywords) > 0


def test_taxonomy_tags_hit() -> None:
    tags = taxonomy_tags("前端开发工程师，熟悉 Vue3 和 TypeScript")
    assert tags, "expected at least one taxonomy hit for a dev JD"
    assert len(tags) == 2  # [level1, level2]


def test_taxonomy_tags_empty() -> None:
    assert taxonomy_tags("") == []
    assert taxonomy_tags("完全无关的文字 没有任何技术关键词") == []


def test_classify_text_tuple_shape() -> None:
    level1, level2 = classify_text("算法工程师 机器学习")
    assert isinstance(level1, str)
    assert isinstance(level2, str)


# ---------------------------------------------------------------------------
# skill_validator
# ---------------------------------------------------------------------------


def test_skill_tags_loads_from_resources() -> None:
    tags = load_skill_tags()
    assert 0 < len(tags) <= 80
    # All tags are non-empty strings.
    assert all(isinstance(t, str) and t for t in tags)


def test_normalize_skill_alias() -> None:
    assert normalize_skill("python") == "Python"
    assert normalize_skill("c++") == "C++"
    assert normalize_skill("llm") == "大模型"


def test_normalize_skill_unknown_is_none() -> None:
    assert normalize_skill("definitely-not-a-real-skill-xyz") is None


def test_validate_skills_basic_dedup_and_low_info_filter() -> None:
    result = validate_skills(
        ["Python", "python", "AI", "技术", "TypeScript"],
    )
    # Case variants dedupe to canonical; low-info labels drop.
    assert "Python" in result
    assert "AI" not in result
    assert "技术" not in result
    # Order preserved for survivors.
    assert result == list(dict.fromkeys(result))


def test_validate_skills_fallback_when_below_min() -> None:
    text = "熟悉 Python 开发，了解 MySQL 数据库"
    result = validate_skills([], fallback_text=text, min_tags=2)
    assert len(result) >= 2
    assert "Python" in result
    assert "MySQL" in result


def test_skills_from_text_order() -> None:
    text = "Python TypeScript Vue"
    tags = skills_from_text(text)
    # Closed-set order, not text order.
    full_set = load_skill_tags()
    indices = [full_set.index(t) for t in tags]
    assert indices == sorted(indices)


# ---------------------------------------------------------------------------
# job_strength
# ---------------------------------------------------------------------------


def test_analyze_job_strength_shape() -> None:
    result = analyze_job_strength(
        "本科及以上学历\n1. 负责前端开发\n2. 参与组件库建设\n3. 性能优化\n"
        "熟悉 Vue3\n优先考虑有经验者"
    )
    assert isinstance(result, JobStrengthResult)
    assert result.tier in {"high", "medium", "low"}
    assert isinstance(result.score, int)
    assert isinstance(result.base_score, int)
    assert isinstance(result.signals, list)
    d = result.to_dict()
    assert set(d.keys()) == {"score", "tier", "base_score", "evidence"}
    assert isinstance(d["evidence"], list)


def test_analyze_job_strength_empty_text() -> None:
    result = analyze_job_strength("")
    assert result.tier == "low"
    assert result.score == 0
    assert result.signals == []


# ---------------------------------------------------------------------------
# batch_progress
# ---------------------------------------------------------------------------


def test_run_parallel_with_progress_order_and_progress() -> None:
    items = ["a", "b", "c", "d"]
    lines: list[str] = []
    results = run_parallel_with_progress(
        items,
        lambda x: x.upper(),
        workers=2,
        label="item",
        progress=lines.append,
    )
    # Deterministic: results sorted by input index.
    assert [r.item for r in results] == items
    assert [r.value for r in results] == ["A", "B", "C", "D"]
    assert all(isinstance(r, BatchResult) for r in results)
    # Progress lines: one per item, monotone counter.
    assert len(lines) == 4
    for i, line in enumerate(lines, 1):
        assert line.startswith(f"{i}/4 done item=")


def test_run_parallel_with_progress_isolates_errors() -> None:
    def _work(x: str) -> str:
        if x == "bad":
            raise ValueError("boom")
        return x

    results = run_parallel_with_progress(
        ["ok", "bad", "also-ok"],
        _work,
        workers=2,
        progress=lambda _s: None,
    )
    assert results[0].value == "ok"
    assert results[0].error is None
    assert results[1].value is None
    assert isinstance(results[1].error, ValueError)
    assert results[2].value == "also-ok"


# ---------------------------------------------------------------------------
# target_evidence
# ---------------------------------------------------------------------------


def test_resolve_target_evidence_raw_with_text() -> None:
    raw = [
        {
            "artifact_id": "art1",
            "source_url": "https://example.com/jobs/1",
            "visible_text": "前端开发工程师 岗位职责 负责 Vue3 开发",
        }
    ]
    result = resolve_target_evidence(raw, [], "art1")
    assert result is not None
    # Raw artifact with visible_text wins — not replaced by candidate path.
    assert result["artifact_id"] == "art1"
    assert "visible_text" in result


def test_resolve_target_evidence_structured_candidate() -> None:
    candidates = [
        {
            "candidate_id": "cand1",
            "artifact_id": "art1",
            "source_url": "https://example.com/jobs/1",
            "title": "前端开发工程师",
            "responsibilities": "负责 Vue3 开发",
            "requirements": "2 年经验",
        }
    ]
    result = resolve_target_evidence(None, candidates, "art1")
    assert result is not None
    assert result["artifact_id"] == "art1"
    assert result["candidate_id"] == "cand1"
    assert result["title"] == "前端开发工程师"
    assert "visible_text" in result
    assert "前端开发工程师" in result["visible_text"]


def test_resolve_target_evidence_recovers_single_candidate_from_stale_pointer() -> None:
    candidates = [
        {
            "candidate_id": "cand1",
            "artifact_id": "art1",
            "title": "前端开发工程师",
            "requirements": "熟悉 Vue3",
        }
    ]
    result = resolve_target_evidence(None, candidates, "stale-model-pointer")
    assert result is not None
    assert result["candidate_id"] == "cand1"
    assert result["artifact_id"] == "art1"


def test_resolve_target_evidence_missing_returns_none() -> None:
    assert resolve_target_evidence([], [], "nonexistent") is None
    assert resolve_target_evidence(None, None, "x") is None
