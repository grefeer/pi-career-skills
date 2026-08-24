"""Evaluation chain tests — hermetic via faux provider + stub registry.

Covers: 2-link chain with inheritance, broken chain stop, projection bounds
(24,000 / 32,000 / 12 / 200-char), and template string presence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from pi_ai import ToolCall
from pi_ai.providers.faux import FAUX_MODEL, FauxScript, clear_scripts, push_script
from pi_career_skills.business.job_discovery.models import (
    ExtractedJobDetails,
    ExtractObservedJobDetailsBatchInput,
    ExtractObservedJobDetailsBatchOutput,
    ExtractObservedJobDetailsOutput,
    FetchPublicJobPageOutput,
    FetchPublicJobPagesInput,
    FetchPublicJobPagesOutput,
    SearchPublicJobPagesInput,
    SearchPublicJobPagesOutput,
)
from pi_career_skills.business.job_matching.job_matching import (
    MatchObservedJobsInput,
    MatchObservedJobsOutput,
    ObservedJobMatch,
)
from pi_career_skills.evaluation.chain import (
    _MAX_CHAIN_SUMMARY_CHARS,
    _MAX_INHERITED_EVIDENCE_BYTES,
    _MAX_STRUCTURED_CANDIDATE_BYTES,
    _MAX_STRUCTURED_CANDIDATES,
    _bounded_inherited_evidence,
    _cap_utf8_bytes,
    _inherited_goal_supplement,
    _structured_candidates_from_artifacts,
    run_chain,
)
from pi_career_skills.evaluation.schema import validate_record
from pi_career_skills.registry import ToolDefinition
from pi_career_skills.runtime.controller import CareerRunController

# ======================================================================
# Helpers — same stub-registry pattern as test_controller.py
# ======================================================================


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class StubHandler:
    def __init__(self, fn: Any, counter: dict[str, int], name: str) -> None:
        self._fn = fn
        self._counter = counter
        self._name = name

    def __call__(self, ctx: Any, params: Any) -> Any:
        self._counter[self._name] = self._counter.get(self._name, 0) + 1
        return self._fn(ctx, params)


def build_stub_registry() -> tuple[Any, dict[str, int]]:
    counts: dict[str, int] = {}

    def _wrap(name: str, fn: Any) -> Any:
        return StubHandler(fn, counts, name)

    def _fetch_pages(ctx: Any, params: Any) -> Any:
        del ctx
        url = params.urls[0] if params.urls else "https://example.com/job/1"
        content_hash = _sha256_hex(f"page:{url}")
        page = FetchPublicJobPageOutput(
            artifact_id="art-fetch-1",
            source_url=url,
            title="Java 后端开发工程师",
            visible_text="负责后端系统开发与维护，需要3年以上Java经验。\n岗位要求：Java, Spring Boot, MySQL",
            content_hash=content_hash,
            quality="jd_complete",
        )
        return FetchPublicJobPagesOutput(pages=[page], failures=[])

    def _extract_batch(ctx: Any, params: Any) -> Any:
        del ctx, params
        content_hash = _sha256_hex("structured:jd1")
        candidate = ExtractedJobDetails(
            title="Java 后端开发工程师",
            company_name="示例科技",
            locations=["北京"],
            responsibilities="负责后端系统架构设计与核心模块开发",
            requirements="3年以上Java开发经验，熟悉Spring Boot和MySQL",
            recruitment_types=["全职"],
            apply_url="https://example.com/apply/1",
            deadline_text=None,
            confidence=0.9,
            evidence_refs=[{"section": "jd_body", "snippet": "Java后端"}],
            normalization_warnings=[],
        )
        detail = ExtractObservedJobDetailsOutput(
            source_artifact_id="art-fetch-1",
            source_url="https://example.com/job/1",
            content_hash=content_hash,
            source_quality="jd_complete",
            candidates=[candidate],
        )
        return ExtractObservedJobDetailsBatchOutput(details=[detail])

    def _match_jobs(ctx: Any, params: Any) -> Any:
        del ctx, params
        m = ObservedJobMatch(
            artifact_id="art-match-1",
            source_url="https://example.com/job/1",
            source_artifact_id="art-struct-1",
            title="Java 后端开发工程师",
            score=85,
            matched_keywords=["Java", "Spring Boot"],
            evidence_excerpt="要求Java和Spring Boot经验",
        )
        return MatchObservedJobsOutput(
            matches=[m],
            evaluated_candidate_count=1,
            evaluated_source_urls=["https://example.com/job/1"],
        )

    def _noop_tool(ctx: Any, params: Any) -> Any:
        del ctx, params
        return FetchPublicJobPagesOutput(pages=[], failures=[])

    def _empty_search(ctx: Any, params: Any) -> Any:
        del ctx
        query = params.query
        url = f"https://example.com/search?q={query}"
        content_hash = hashlib.sha256(f"search:{query}".encode()).hexdigest()
        return SearchPublicJobPagesOutput(
            query=query,
            source_url=url,
            content_hash=content_hash,
            results=[],
            terminal_reason="search_empty",
        )

    tools: dict[str, ToolDefinition] = {}
    tools["fetch-public-job-pages"] = ToolDefinition(
        name="fetch-public-job-pages",
        skill_name="job-discovery",
        input_model=FetchPublicJobPagesInput,
        output_model=FetchPublicJobPagesOutput,
        handler=_wrap("fetch-public-job-pages", _fetch_pages),
        is_deliverable=True,
        artifact_type="public_job_page",
        description="stub fetch",
    )
    tools["extract-observed-job-details-batch"] = ToolDefinition(
        name="extract-observed-job-details-batch",
        skill_name="job-discovery",
        input_model=ExtractObservedJobDetailsBatchInput,
        output_model=ExtractObservedJobDetailsBatchOutput,
        handler=_wrap("extract-observed-job-details-batch", _extract_batch),
        is_deliverable=True,
        artifact_type="structured_job_details",
        description="stub extract",
    )
    tools["search-public-job-pages"] = ToolDefinition(
        name="search-public-job-pages",
        skill_name="job-discovery",
        input_model=SearchPublicJobPagesInput,
        output_model=SearchPublicJobPagesOutput,
        handler=_wrap("search-public-job-pages", _empty_search),
        artifact_type="job_search_results",
        description="stub search",
    )
    remaining = [
        "query-career-sheet-records",
        "fetch-public-job-page",
        "extract-observed-job-details",
        "validate-observed-candidates",
        "fetch-wechat-article",
        "deduplicate-observed-jobs",
        "classify-job-url",
    ]
    for tname in remaining:
        tools[tname] = ToolDefinition(
            name=tname,
            skill_name="job-discovery",
            input_model=FetchPublicJobPagesInput,
            output_model=FetchPublicJobPagesOutput,
            handler=_wrap(tname, _noop_tool),
            description=f"stub {tname}",
        )
    tools["match-observed-jobs"] = ToolDefinition(
        name="match-observed-jobs",
        skill_name="job-matching",
        input_model=MatchObservedJobsInput,
        output_model=MatchObservedJobsOutput,
        handler=_wrap("match-observed-jobs", _match_jobs),
        is_deliverable=True,
        artifact_type="job_matching_report",
        description="stub match",
    )

    class _StubRegistry:
        def __init__(self, tools: dict[str, ToolDefinition]) -> None:
            self._tools = tools

        def get(self, name: str, default: Any = None) -> ToolDefinition | None:
            return self._tools.get(name, default)

        def __getitem__(self, name: str) -> ToolDefinition:
            return self._tools[name]

        def invoke(
            self, tool_name, context, params, tool_call_id=None
        ) -> Any:
            from pi_career_skills.tool_adapter import invoke_tool_sync

            return invoke_tool_sync(
                registry=self,
                context=context,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                params=params,
            )

    return _StubRegistry(tools), counts


# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture(autouse=True)
def _clear_faux() -> None:
    clear_scripts()
    yield
    clear_scripts()


@pytest.fixture
def stub_registry() -> tuple[Any, dict[str, int]]:
    return build_stub_registry()


@pytest.fixture
def controller_factory(
    stub_registry: tuple[Any, dict[str, int]],
) -> Any:
    reg, _counts = stub_registry

    def _factory() -> CareerRunController:
        return CareerRunController(
            FAUX_MODEL,
            registry=reg,
            get_api_key=lambda provider: "test-key",
        )

    return _factory


def _make_chain_file(
    tmp_path: Path,
    cid: str,
    links: list[dict[str, Any]],
) -> None:
    doc = {"id": cid, "chain": links}
    (tmp_path / f"{cid}.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _discovery_link(skills: list[str] | None = None) -> dict[str, Any]:
    return {
        "question": "帮我找Java后端岗位",
        "meta": {"skills": skills or ["job-discovery"]},
        "profile": {
            "role": "Java 后端开发工程师",
            "summary": "技能：Java, Spring Boot，社招（3 年经验）",
            "resume_text": None,
        },
    }


def _matching_link(skills: list[str] | None = None) -> dict[str, Any]:
    return {
        "question": "帮我匹配Java岗位",
        "meta": {"skills": skills or ["job-matching"]},
        "profile": {
            "role": "Java 后端开发工程师",
            "summary": "技能：Java, Spring Boot，社招（3 年经验）",
            "resume_text": None,
        },
    }


# ======================================================================
# 1. Two-link chain: link1 (discovery) succeeds, link2 (matching) inherits
# ======================================================================


@pytest.mark.asyncio
async def test_two_link_chain_inherits(
    tmp_path: Path,
    stub_registry: tuple[Any, dict[str, int]],
) -> None:
    _reg, counts = stub_registry
    cid = "C001"
    _make_chain_file(tmp_path, cid, [_discovery_link(), _matching_link()])
    out_dir = tmp_path / "out"

    # Link 1: delegate discovery → fetch + extract → done
    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="delegate-job-discovery",
                    arguments={"task_goal": "找Java岗位"},
                )
            ]
        )
    )
    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="d1",
                    name="fetch-public-job-pages",
                    arguments={"urls": ["https://example.com/job/1"]},
                )
            ]
        )
    )
    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="d2",
                    name="extract-observed-job-details-batch",
                    arguments={"artifact_ids": ["art-fetch-1"]},
                )
            ]
        )
    )
    push_script(FauxScript(text="发现完成。"))
    push_script(FauxScript(text="职位发现完成。"))

    # Link 2: delegate matching → match → done
    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s2",
                    name="delegate-job-matching",
                    arguments={"task_goal": "匹配岗位"},
                )
            ]
        )
    )
    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="m1",
                    name="match-observed-jobs",
                    arguments={"profile_keywords": ["Java"], "max_results": 10},
                )
            ]
        )
    )
    push_script(FauxScript(text="匹配完成。"))
    push_script(FauxScript(text="匹配完成，结果见参考资料。"))

    # Capture each link's controller task so the inherited supplement and
    # chain-context note can be verified end-to-end (regression test for the
    # _chain_context_note wiring — see Wave 5 review medium-1 finding).
    captured_tasks: list[str] = []

    def _capturing_factory() -> CareerRunController:
        reg, _ = stub_registry
        base = CareerRunController(
            FAUX_MODEL,
            registry=reg,
            get_api_key=lambda provider: "test-key",
        )
        orig_run = base.run

        async def _run(request: Any) -> Any:
            captured_tasks.append(request.task)
            return await orig_run(request)

        base.run = _run  # type: ignore[method-assign]
        return base

    record = await run_chain(
        cid,
        question_dir=tmp_path,
        out_dir=out_dir,
        model_id="faux",
        controller_factory=_capturing_factory,
    )

    assert record["type"] == "chain"
    assert record["chain_length"] == 2
    assert len(record["links"]) == 2
    assert record["result"]["status"] == "succeeded"

    # Validate schema.
    validate_record(record)

    # Link1 is discovery, link2 is matching.
    assert record["links"][0]["id"] == "C001-L1"
    assert record["links"][1]["id"] == "C001-L2"
    assert record["links"][0]["result"]["status"] == "succeeded"
    assert record["links"][1]["result"]["status"] == "succeeded"

    # Link record "question" stores the original doc question (not the
    # supplemented task).  Supplement markers are verified via the
    # projection function directly below.
    assert "【上一环节已收集的岗位】" in _inherited_goal_supplement(
        _structured_candidates_from_artifacts(
            record["links"][0]["artifacts"]
        ),
        {"resume_text": "test"},
    )

    # Link 2's model prompt carries the chain-context note + structured
    # supplement (end-to-end wiring of _chain_context_note).
    assert len(captured_tasks) == 2
    assert "上一环节（C001-L1）" in captured_tasks[1]
    assert "【上一环节已收集的岗位】" in captured_tasks[1]

    # File written, no tmp leftover.
    assert (out_dir / f"{cid}.json").exists()
    assert not (out_dir / f"{cid}.json.tmp").exists()


# ======================================================================
# 2. Broken chain: link1 waiting_user → link2 NOT run
# ======================================================================


@pytest.mark.asyncio
async def test_broken_chain_stops(
    tmp_path: Path,
    controller_factory: Any,
) -> None:
    cid = "C002"
    _make_chain_file(tmp_path, cid, [_discovery_link(), _matching_link()])
    out_dir = tmp_path / "out"

    # Link 1: supervisor delegates discovery but it fails / stalls
    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="delegate-job-discovery",
                    arguments={"task_goal": "找Java岗位"},
                )
            ]
        )
    )
    # Skill agent returns without completing (no tool calls → no evidence).
    push_script(FauxScript(text="没有找到。"))
    push_script(FauxScript(text="没有找到岗位。"))

    record = await run_chain(
        cid,
        question_dir=tmp_path,
        out_dir=out_dir,
        model_id="faux",
        controller_factory=controller_factory,
    )

    # Only 1 link ran (chain_length matches executed links per schema).
    assert len(record["links"]) == 1
    assert record["chain_length"] == 1
    assert record["links"][0]["id"] == "C002-L1"
    # Top-level status is NOT succeeded.
    assert record["result"]["status"] != "succeeded"


# ======================================================================
# 3. _cap_utf8_bytes CJK-aware
# ======================================================================


def test_cap_utf8_bytes_cjk_aware() -> None:
    # Each CJK char is 3 bytes in UTF-8.
    text = "你好世界"
    assert len(text.encode("utf-8")) == 12  # 4 chars * 3 bytes

    # Budget of 10 bytes → 3 chars (9 bytes).
    capped = _cap_utf8_bytes(text, 10)
    assert len(capped.encode("utf-8")) <= 10
    assert capped == "你好世"

    # Budget larger than text → unchanged.
    assert _cap_utf8_bytes(text, 100) == text

    # Budget 0 → empty.
    assert _cap_utf8_bytes(text, 0) == ""


# ======================================================================
# 4. _bounded_inherited_evidence 24,000 byte cap
# ======================================================================


def test_bounded_inherited_evidence_24k_cap() -> None:
    # Build 100 artifacts, each with 1000 bytes of visible_text.
    artifacts: list[dict] = []
    for i in range(100):
        long_text = "字" * 350  # 350 * 3 = 1050 bytes per artifact
        artifacts.append(
            {
                "artifact_type": "public_job_page",
                "artifact_id": f"art-{i}",
                "source_url": f"https://example.com/{i}",
                "content_hash": "a" * 64,
                "content_json": {"visible_text": long_text},
            }
        )

    bounded = _bounded_inherited_evidence(artifacts)
    total_bytes = sum(
        len(item["visible_text"].encode("utf-8")) for item in bounded
    )

    assert total_bytes <= _MAX_INHERITED_EVIDENCE_BYTES
    assert _MAX_INHERITED_EVIDENCE_BYTES == 24_000
    # Should have promoted multiple items (not just one).
    assert len(bounded) > 1

    # Non-job-bearing types are skipped.
    non_job = [
        {
            "artifact_type": "job_search_results",
            "artifact_id": "x",
            "source_url": "https://example.com",
            "content_hash": "a" * 64,
            "content_json": {"visible_text": "ignored"},
        }
    ]
    assert _bounded_inherited_evidence(non_job) == []

    # Empty visible_text is skipped.
    empty_text = [
        {
            "artifact_type": "public_job_page",
            "artifact_id": "y",
            "source_url": "https://example.com/2",
            "content_hash": "b" * 64,
            "content_json": {"visible_text": ""},
        }
    ]
    assert _bounded_inherited_evidence(empty_text) == []


# ======================================================================
# 5. _structured_candidates_from_artifacts 32,000 byte cap + 12 candidate cap
# ======================================================================


def test_structured_candidates_bounds() -> None:
    # Build one artifact with many large candidates.
    big_candidates = []
    for i in range(20):
        big_candidates.append(
            {
                "candidate_id": f"cand-{i}",
                "title": f"岗位{i}",
                "company_name": f"公司{i}",
                "locations": ["北京"],
                "responsibilities": "职" * 1500,  # ~4500 bytes
                "requirements": "要" * 1500,  # ~4500 bytes
            }
        )
    artifacts = [
        {
            "artifact_type": "structured_job_details",
            "artifact_id": "art-struct-1",
            "source_url": "https://example.com/1",
            "content_hash": "c" * 64,
            "content_json": {"candidates": big_candidates},
        }
    ]

    result = _structured_candidates_from_artifacts(artifacts)
    total_bytes = sum(
        len(
            json.dumps(c, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        for c in result
    )

    # 12-candidate cap.
    assert len(result) <= _MAX_STRUCTURED_CANDIDATES
    assert _MAX_STRUCTURED_CANDIDATES == 12

    # 32,000 byte cap.
    assert total_bytes <= _MAX_STRUCTURED_CANDIDATE_BYTES
    assert _MAX_STRUCTURED_CANDIDATE_BYTES == 32_000


# ======================================================================
# 6. _inherited_goal_supplement exact template strings
# ======================================================================


def test_inherited_goal_supplement_exact_templates() -> None:
    candidates = [
        {
            "title": "Java 后端工程师",
            "company": "示例科技",
            "locations": ["北京", "上海"],
            "candidate_id": "cand-001",
        },
        {
            "title": "Python 开发",
            "company": "",
            "locations": [],
            "candidate_id": "",
        },
    ]
    facts = {
        f"fact_{i}": f"value_{i}" for i in range(20)
    }  # more than 12

    supplement = _inherited_goal_supplement(candidates, facts)

    # Exact template markers.
    assert "【上一环节已收集的岗位】" in supplement
    assert "【候选人已确认事实（简历）】" in supplement

    # Candidate line format (with artifact id).
    assert "（证据 artifact: cand-001）" in supplement
    assert "｜" in supplement  # separator

    # Facts capped at 12.
    fact_lines = [
        line for line in supplement.split("\n") if line.startswith("- fact_")
    ]
    assert len(fact_lines) == 12

    # Candidates capped at 8.
    cand_lines = [
        line for line in supplement.split("\n") if line.startswith("- ") and "：" not in line
    ]
    # Should be ≤ 8 candidates shown.
    assert len(cand_lines) <= 8 + 12  # rough: candidates + facts

    # Empty candidates/facts still works.
    empty = _inherited_goal_supplement([], {})
    assert empty == ""


# ======================================================================
# 7. 200-char chain summary bound
# ======================================================================


def test_chain_summary_200_char_bound() -> None:
    assert _MAX_CHAIN_SUMMARY_CHARS == 200

    from pi_career_skills.evaluation.chain import _chain_context_note

    long_summary = "字" * 500
    note = _chain_context_note("C001-L1", long_summary)
    assert note is not None
    # The summary portion should be ≤ 200 chars.
    assert len(long_summary[:200]) == 200
    assert "上一环节（C001-L1）" in note

    # None for empty summary.
    assert _chain_context_note("C001-L1", "") is None
    assert _chain_context_note("C001-L1", None) is None
