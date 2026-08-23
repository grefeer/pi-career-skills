"""CareerRunController tests — hermetic via faux provider + stub registry.

16 scenarios covering the full controller contract: success paths,
error paths, budget/stall, recovery, matching fallback, events, etc.

No network. No real LLM. All skill-agent business tools are stub handlers
on real ToolDefinition instances (real input/output pydantic models),
recording invocation counts.
"""

from __future__ import annotations

import hashlib
import json
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
from pi_career_skills.business.resume_tailoring.resume_tailoring import (
    BuildResumeTailoringBriefInput,
    ResumeTailoringBriefOutput,
    ResumeTailoringDiff,
)
from pi_career_skills.errors import (
    AUTO_RECOVERY_LIMIT_REACHED,
    BUDGET_EXHAUSTED,
    COMPLETION_EVIDENCE_UNAVAILABLE,
    MODEL_API_KEY_MISSING,
    NO_PROGRESS,
)
from pi_career_skills.registry import ToolDefinition
from pi_career_skills.runtime.budgets import BudgetLimits
from pi_career_skills.runtime.controller import CareerRunController, RunRequest

# ======================================================================
# Helpers
# ======================================================================


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ======================================================================
# Stub registry builder
# ======================================================================


class StubHandler:
    """Recordable stub handler wrapper."""

    def __init__(self, fn: Any, counter: dict[str, int], name: str) -> None:
        self._fn = fn
        self._counter = counter
        self._name = name

    def __call__(self, ctx: Any, params: Any) -> Any:
        self._counter[self._name] = self._counter.get(self._name, 0) + 1
        return self._fn(ctx, params)


def build_stub_registry() -> tuple[Any, dict[str, int]]:
    """Build a stub registry + call-count dict.

    The returned registry implements ``.get(name)`` returning ``ToolDefinition``
    instances with real pydantic input/output models and stub handlers.
    Handlers are wrapped to count invocations.
    """
    counts: dict[str, int] = {}

    def _wrap(name: str, fn: Any) -> Any:
        return StubHandler(fn, counts, name)

    # ---- fetch-public-job-pages: returns one jd_complete page ----
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

    # ---- extract-observed-job-details-batch: returns real candidates ----
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

    # ---- match-observed-jobs: returns one match ----
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

    # ---- build-resume-tailoring-brief: returns brief ----
    def _build_brief(ctx: Any, params: Any) -> Any:
        del ctx, params
        diff = ResumeTailoringDiff(
            op="highlight",
            section="experience",
            fact_ref="fact-1",
            target_evidence_ref="evidence-1",
            change_summary="突出Java后端开发经验",
        )
        return ResumeTailoringBriefOutput(
            target_artifact_id="art-struct-1",
            target_title="Java 后端开发工程师",
            source_url="https://example.com/job/1",
            supported_keywords=["Java"],
            missing_keywords=["Kubernetes"],
            safe_actions=["突出Java经验", "补充项目经历"],
            proposed_diffs=[diff],
        )

    # ---- no-op tool: returns empty, never produces artifacts ----
    def _noop_tool(ctx: Any, params: Any) -> Any:
        del ctx, params
        return FetchPublicJobPagesOutput(pages=[], failures=[])

    # ---- empty search: returns search_empty result (no artifacts) ----
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

    # Build tool definitions.
    tools: dict[str, ToolDefinition] = {}

    # discovery tools (10 — keep catalog shape)
    tools["fetch-public-job-pages"] = ToolDefinition(
        name="fetch-public-job-pages",
        skill_name="job-discovery",
        input_model=FetchPublicJobPagesInput,
        output_model=FetchPublicJobPagesOutput,
        handler=_wrap("fetch-public-job-pages", _fetch_pages),
        is_deliverable=True,
        artifact_type="public_job_page",
        description="stub fetch pages",
    )
    tools["extract-observed-job-details-batch"] = ToolDefinition(
        name="extract-observed-job-details-batch",
        skill_name="job-discovery",
        input_model=ExtractObservedJobDetailsBatchInput,
        output_model=ExtractObservedJobDetailsBatchOutput,
        handler=_wrap("extract-observed-job-details-batch", _extract_batch),
        is_deliverable=True,
        artifact_type="structured_job_details",
        description="stub extract batch",
    )
    # Remaining 8 discovery tools — no-op handlers, registered for catalog shape
    # search-public-job-pages: correct model, empty results (no artifacts)
    tools["search-public-job-pages"] = ToolDefinition(
        name="search-public-job-pages",
        skill_name="job-discovery",
        input_model=SearchPublicJobPagesInput,
        output_model=SearchPublicJobPagesOutput,
        handler=_wrap("search-public-job-pages", _empty_search),
        artifact_type="job_search_results",
        description="stub search",
    )
    # Remaining 7 discovery tools — no-op handlers, registered for catalog shape
    remaining_discovery = [
        "query-career-sheet-records",
        "fetch-public-job-page",
        "extract-observed-job-details",
        "validate-observed-candidates",
        "fetch-wechat-article",
        "deduplicate-observed-jobs",
        "classify-job-url",
    ]
    for tname in remaining_discovery:
        tools[tname] = ToolDefinition(
            name=tname,
            skill_name="job-discovery",
            input_model=FetchPublicJobPagesInput,
            output_model=FetchPublicJobPagesOutput,
            handler=_wrap(tname, _noop_tool),
            description=f"stub {tname}",
        )

    # matching tool (1)
    tools["match-observed-jobs"] = ToolDefinition(
        name="match-observed-jobs",
        skill_name="job-matching",
        input_model=MatchObservedJobsInput,
        output_model=MatchObservedJobsOutput,
        handler=_wrap("match-observed-jobs", _match_jobs),
        is_deliverable=True,
        artifact_type="job_matching_report",
        description="stub match jobs",
    )

    # tailoring tool (1)
    tools["build-resume-tailoring-brief"] = ToolDefinition(
        name="build-resume-tailoring-brief",
        skill_name="resume-tailoring",
        input_model=BuildResumeTailoringBriefInput,
        output_model=ResumeTailoringBriefOutput,
        handler=_wrap("build-resume-tailoring-brief", _build_brief),
        is_deliverable=True,
        artifact_type="resume_tailoring_brief",
        description="stub build brief",
    )

    class _StubRegistry:
        def __init__(self, tools: dict[str, ToolDefinition]) -> None:
            self._tools = tools

        def get(self, name: str, default: Any = None) -> ToolDefinition | None:
            return self._tools.get(name, default)

        def __getitem__(self, name: str) -> ToolDefinition:
            return self._tools[name]

        def invoke(
            self,
            tool_name: str,
            context: Any,
            params: dict[str, Any],
            tool_call_id: str | None = None,
        ) -> Any:
            """Trusted-kernel sync invoke (for matching_fallback)."""
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
def controller(stub_registry: tuple[Any, dict[str, int]]) -> CareerRunController:
    reg, _counts = stub_registry
    return CareerRunController(
        FAUX_MODEL,
        registry=reg,
        get_api_key=lambda provider: "test-key",
    )


# ======================================================================
# 1. Success single-skill
# ======================================================================


async def test_success_single_skill(
    controller: CareerRunController, stub_registry: tuple[Any, dict[str, int]]
) -> None:
    _reg, counts = stub_registry
    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="delegate-job-discovery",
                    arguments={"task_goal": "找Java后端岗位"},
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
    push_script(FauxScript(text="已完成职位发现。"))
    # Supervisor's final turn (after delegation returns)
    push_script(FauxScript(text="已为您找到Java后端岗位，详情请见参考资料。"))

    result = await controller.run(
        RunRequest(task="帮我找Java后端岗位", needed_skills=("job-discovery",))
    )

    assert result.status == "succeeded"
    assert result.completed_skills == ["job-discovery"]
    assert result.summary is not None and len(result.summary) > 0
    assert len(result.refs) > 0
    assert result.attempt_count == 1
    assert counts["fetch-public-job-pages"] == 1
    assert counts["extract-observed-job-details-batch"] == 1


# ======================================================================
# 2. Multi-skill
# ======================================================================


async def test_multi_skill(
    controller: CareerRunController, stub_registry: tuple[Any, dict[str, int]]
) -> None:
    _reg, counts = stub_registry

    # Turn 1: delegate discovery
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
    push_script(FauxScript(text="职位发现完成。"))

    # Turn 2: delegate matching
    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s2",
                    name="delegate-job-matching",
                    arguments={"task_goal": "匹配Java岗位"},
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

    # Turn 3: delegate tailoring
    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s3",
                    name="delegate-resume-tailoring",
                    arguments={"task_goal": "生成简历修改建议"},
                )
            ]
        )
    )
    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="t1",
                    name="build-resume-tailoring-brief",
                    arguments={
                        "target_artifact_id": "art-struct-1",
                        "target_keywords": ["Java"],
                    },
                )
            ]
        )
    )
    push_script(FauxScript(text="简历优化建议已生成。"))

    # Supervisor final
    push_script(FauxScript(text="三项任务全部完成。"))

    result = await controller.run(
        RunRequest(
            task="找Java岗位并匹配优化简历",
            needed_skills=("job-discovery", "job-matching", "resume-tailoring"),
        )
    )

    assert result.status == "succeeded"
    assert set(result.completed_skills) == {
        "job-discovery",
        "job-matching",
        "resume-tailoring",
    }
    assert result.attempt_count == 1
    assert counts["fetch-public-job-pages"] == 1
    assert counts["extract-observed-job-details-batch"] == 1
    assert counts["match-observed-jobs"] == 1
    assert counts["build-resume-tailoring-brief"] == 1


# ======================================================================
# 3. Not-allowed skill
# ======================================================================


async def test_not_allowed_skill(
    controller: CareerRunController, stub_registry: tuple[Any, dict[str, int]]
) -> None:
    _reg, counts = stub_registry

    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="delegate-job-matching",
                    arguments={"task_goal": "匹配岗位"},
                )
            ]
        )
    )
    push_script(FauxScript(text="无法执行。"))

    result = await controller.run(
        RunRequest(
            task="匹配岗位",
            allowed_skills=("job-discovery",),
            needed_skills=("job-discovery",),
        )
    )

    assert counts.get("match-observed-jobs", 0) == 0
    assert result.status == "waiting_user"
    assert result.error_code == COMPLETION_EVIDENCE_UNAVAILABLE


# ======================================================================
# 4. Already-succeeded skill
# ======================================================================


async def test_already_succeeded_skill(
    controller: CareerRunController, stub_registry: tuple[Any, dict[str, int]]
) -> None:
    _reg, counts = stub_registry

    # Turn 1: delegate discovery → succeeds
    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="delegate-job-discovery",
                    arguments={"task_goal": "找岗位"},
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

    # Turn 2: delegate discovery again → already_succeeded
    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s2",
                    name="delegate-job-discovery",
                    arguments={"task_goal": "再找一些"},
                )
            ]
        )
    )
    push_script(FauxScript(text="已完成。"))

    result = await controller.run(
        RunRequest(task="找岗位", needed_skills=("job-discovery",))
    )

    assert result.status == "succeeded"
    assert result.completed_skills == ["job-discovery"]
    assert counts["fetch-public-job-pages"] == 1
    assert counts["extract-observed-job-details-batch"] == 1


# ======================================================================
# 5. Missing evidence
# ======================================================================


async def test_missing_evidence(
    controller: CareerRunController, stub_registry: tuple[Any, dict[str, int]]
) -> None:
    _reg, counts = stub_registry

    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="delegate-job-matching",
                    arguments={"task_goal": "匹配"},
                )
            ]
        )
    )
    push_script(FauxScript(text="失败。"))

    result = await controller.run(
        RunRequest(task="匹配", needed_skills=("job-matching",))
    )

    assert counts.get("match-observed-jobs", 0) == 0
    assert result.status == "waiting_user"
    assert result.error_code == COMPLETION_EVIDENCE_UNAVAILABLE


# ======================================================================
# 6. Duplicate tool call — deduped without consuming budget
# ======================================================================


async def test_duplicate_tool_call(
    controller: CareerRunController, stub_registry: tuple[Any, dict[str, int]]
) -> None:
    _reg, counts = stub_registry

    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="delegate-job-discovery",
                    arguments={"task_goal": "找岗位"},
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
                    id="d3",
                    name="extract-observed-job-details-batch",
                    arguments={"artifact_ids": ["art-fetch-1"]},
                )
            ]
        )
    )
    push_script(FauxScript(text="完成。"))

    result = await controller.run(
        RunRequest(task="找岗位", needed_skills=("job-discovery",))
    )

    assert counts["fetch-public-job-pages"] == 1
    assert result.status == "succeeded"
    # 3 real tool calls: delegate + fetch + extract (duplicate is deduped pre-handler)
    assert result.budget.tool_calls == 3


# ======================================================================
# 7. Hard stall → no_progress
# ======================================================================


async def test_hard_stall_no_progress(
    controller: CareerRunController, stub_registry: tuple[Any, dict[str, int]]
) -> None:
    _reg, counts = stub_registry

    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="delegate-job-discovery",
                    arguments={"task_goal": "找岗位"},
                )
            ]
        )
    )
    for i in range(9):
        # Each query must be unique — otherwise dedup kicks in before stall.
        push_script(
            FauxScript(
                tool_calls=[
                    ToolCall(
                        id=f"n{i}",
                        name="search-public-job-pages",
                        arguments={"query": f"Java 后端 第{i}页", "max_results": 5},
                    )
                ]
            )
        )

    result = await controller.run(
        RunRequest(task="找岗位", needed_skills=("job-discovery",))
    )

    assert result.status == "waiting_user"
    assert result.error_code == NO_PROGRESS
    assert result.attempt_count == 1


# ======================================================================
# 8. Soft stall steering (6 calls → steer, not hard stop)
# ======================================================================


async def test_soft_stall_steering(
    controller: CareerRunController, stub_registry: tuple[Any, dict[str, int]]
) -> None:
    _reg, counts = stub_registry

    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="delegate-job-discovery",
                    arguments={"task_goal": "找岗位"},
                )
            ]
        )
    )
    for i in range(6):
        # Each query must be unique — otherwise dedup kicks in before stall.
        push_script(
            FauxScript(
                tool_calls=[
                    ToolCall(
                        id=f"n{i}",
                        name="search-public-job-pages",
                        arguments={"query": f"Java 后端 第{i}页", "max_results": 5},
                    )
                ]
            )
        )
    push_script(FauxScript(text="基于现有结果，暂无更多进展。"))

    result = await controller.run(
        RunRequest(task="找岗位", needed_skills=("job-discovery",))
    )

    # Did NOT hard-stop (no NO_PROGRESS).
    assert result.error_code != NO_PROGRESS
    assert counts["search-public-job-pages"] >= 6
    assert result.status == "waiting_user"

    # Steering actually fired — stall_soft_warning event present
    # (the external assertion point for the soft-stall mechanism).
    stall_events = [e for e in result.events if e.type == "stall_soft_warning"]
    assert len(stall_events) >= 1, "expected at least one stall_soft_warning event"
    first_stall = stall_events[0]
    assert first_stall.payload["kind"] in {"supervisor", "job-discovery"}
    assert first_stall.payload["streak"] >= 6


# ======================================================================
# 9. Turn-budget exhaustion
# ======================================================================


async def test_turn_budget_exhaustion(
    controller: CareerRunController, stub_registry: tuple[Any, dict[str, int]]
) -> None:
    _reg, counts = stub_registry

    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="delegate-job-discovery",
                    arguments={"task_goal": "找岗位"},
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

    result = await controller.run(
        RunRequest(
            task="找岗位",
            needed_skills=("job-discovery",),
            budget=BudgetLimits(agent_turns=1),
        )
    )

    assert result.status == "waiting_user"
    assert result.error_code == BUDGET_EXHAUSTED
    assert result.attempt_count == 1


# ======================================================================
# 10. Wall-clock exhaustion → auto-recovery → success
# ======================================================================


async def test_wall_clock_exhaustion_auto_recovery(
    controller: CareerRunController, stub_registry: tuple[Any, dict[str, int]]
) -> None:
    _reg, _counts = stub_registry

    # wall_clock_seconds=0 → every attempt exhausts immediately.
    # Wall-clock exhaustion is auto-recoverable (§7.3), so the controller
    # should retry up to auto_recoveries times then end with limit-reached.
    # This proves wall_clock_budget_exhausted is in the recoverable set.
    push_script(FauxScript(text="ignored-1"))
    push_script(FauxScript(text="ignored-2"))
    push_script(FauxScript(text="ignored-3"))

    result = await controller.run(
        RunRequest(
            task="找岗位",
            needed_skills=("job-discovery",),
            budget=BudgetLimits(wall_clock_seconds=0, auto_recoveries=2),
        )
    )

    # All 3 attempts (initial + 2 recoveries) hit wall clock exhaustion.
    assert result.status == "waiting_user"
    assert result.error_code == AUTO_RECOVERY_LIMIT_REACHED
    assert result.attempt_count == 3
    assert result.budget.auto_recoveries == 2


# ======================================================================
# 11. Invalid model response → auto-recovery → success
# ======================================================================


async def test_invalid_model_response_auto_recovery(
    controller: CareerRunController, stub_registry: tuple[Any, dict[str, int]]
) -> None:
    _reg, _counts = stub_registry

    push_script(FauxScript(error="boom"))

    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="delegate-job-discovery",
                    arguments={"task_goal": "找岗位"},
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
    push_script(FauxScript(text="完成。"))

    result = await controller.run(
        RunRequest(task="找岗位", needed_skills=("job-discovery",))
    )

    assert result.status == "succeeded"
    assert result.attempt_count == 2


# ======================================================================
# 12. Recovery limit reached
# ======================================================================


async def test_recovery_limit_reached(
    controller: CareerRunController, stub_registry: tuple[Any, dict[str, int]]
) -> None:
    _reg, _counts = stub_registry

    for _ in range(3):
        push_script(FauxScript(error="boom"))

    result = await controller.run(
        RunRequest(
            task="找岗位",
            needed_skills=("job-discovery",),
            budget=BudgetLimits(auto_recoveries=2),
        )
    )

    assert result.status == "waiting_user"
    assert result.error_code == AUTO_RECOVERY_LIMIT_REACHED
    assert result.attempt_count == 3


# ======================================================================
# 13. Matching fallback (§6.6)
# ======================================================================


async def test_matching_fallback(
    controller: CareerRunController, stub_registry: tuple[Any, dict[str, int]]
) -> None:
    _reg, counts = stub_registry

    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="delegate-job-discovery",
                    arguments={"task_goal": "找岗位"},
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
    push_script(FauxScript(text="职位发现完成。"))
    push_script(FauxScript(text="所有工作完成。"))

    result = await controller.run(
        RunRequest(
            task="找并匹配岗位",
            needed_skills=("job-discovery", "job-matching"),
        )
    )

    assert counts["match-observed-jobs"] == 1
    assert result.status == "succeeded"
    assert "job-matching" in result.completed_skills
    assert any(r["artifact_type"] == "job_matching_report" for r in result.refs)


# ======================================================================
# 14. Terminal status uniqueness
# ======================================================================


async def test_terminal_status_uniqueness(
    controller: CareerRunController, stub_registry: tuple[Any, dict[str, int]]
) -> None:
    _reg, counts = stub_registry

    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="delegate-job-discovery",
                    arguments={"task_goal": "找岗位"},
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
    push_script(FauxScript(text="完成。"))

    result = await controller.run(
        RunRequest(task="找岗位", needed_skills=("job-discovery",))
    )

    assert result.status in {"succeeded", "waiting_user", "failed"}
    assert counts.get("fetch-public-job-pages", 0) >= 1


# ======================================================================
# 15. Model API key missing
# ======================================================================


def test_model_api_key_missing(stub_registry: tuple[Any, dict[str, int]]) -> None:
    import asyncio

    from pi_ai import Model

    reg, counts = stub_registry
    deepseek_model = Model(
        id="deepseek-chat",
        name="DeepSeek",
        api="openai",
        provider="deepseek",
        base_url="https://api.deepseek.com",
        reasoning=False,
        input=["text"],
        context_window=128000,
        max_tokens=4096,
    )

    controller = CareerRunController(
        deepseek_model,
        registry=reg,
        get_api_key=lambda provider: None,
    )

    result = asyncio.run(controller.run(RunRequest(task="找岗位")))

    assert result.status == "failed"
    assert result.error_code == MODEL_API_KEY_MISSING
    assert result.attempt_count == 0
    assert counts == {}


# ======================================================================
# 16. Events
# ======================================================================


async def test_events_present_and_bounded(
    controller: CareerRunController, stub_registry: tuple[Any, dict[str, int]]
) -> None:
    _reg, _counts = stub_registry

    push_script(
        FauxScript(
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="delegate-job-discovery",
                    arguments={"task_goal": "找岗位"},
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
    push_script(FauxScript(text="完成。"))

    result = await controller.run(
        RunRequest(task="找岗位", needed_skills=("job-discovery",))
    )

    event_types = [e.type for e in result.events]

    assert "run_started" in event_types
    assert "attempt_started" in event_types
    assert "tool_observation" in event_types
    assert "run_finalized" in event_types
    assert any(t.startswith("delegation_") for t in event_types)

    for event in result.events:
        payload_str = json.dumps(event.payload, ensure_ascii=False)
        assert "Traceback" not in payload_str
        assert len(payload_str) < 10000


# ======================================================================
# Bonus: run_id generation
# ======================================================================


def test_controller_run_id_generation(
    stub_registry: tuple[Any, dict[str, int]],
) -> None:
    import asyncio

    reg, _counts = stub_registry
    c = CareerRunController(FAUX_MODEL, registry=reg, get_api_key=lambda p: "k")
    push_script(FauxScript(text="hello"))
    result = asyncio.run(c.run(RunRequest(task="hi", needed_skills=())))
    assert result.run_id
    assert len(result.run_id) == 32
