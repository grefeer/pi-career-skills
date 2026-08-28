"""Registered career-skill tool definitions and the trusted-kernel registry.

Port of ``backend/app/services/career_skills/registry.py`` from the source
project.  The 13 tool descriptions are copied **verbatim** or derived from
the approved archived skill contract — they are
model-visible contract text (the subagent prompts name the same tools) and
must not drift from the source.

The registry is the single source of truth for:

* tool identity (``name`` / ``skill_name`` / ``is_deliverable``),
* the persisted artifact type per deliverable tool (``TOOL_ARTIFACT_TYPE``),
* the per-skill catalog (``TOOL_CATALOG_BY_SKILL`` — 11 / 2 / 2 / 2),
* the deterministic invoke entry (``CareerToolRegistry.invoke``) used by
  the trusted kernel (matching fallback, controller orchestration).

The model-facing path is ``tool_adapter.invoke_tool`` / ``make_agent_tool``;
it re-uses these definitions but adds skill isolation and runs handlers off
the event loop.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from .business.career_planning import (
    BuildPreparationPlanInput,
    CareerPreparationPlanOutput,
    build_preparation_plan,
)
from .business.job_discovery.handlers import (
    deduplicate_observed_jobs,
    extract_observed_job_details,
    extract_observed_job_details_batch,
    validate_observed_candidates,
)
from .business.job_discovery.models import (
    BrowsePublicJobPageInput,
    BrowsePublicJobPageOutput,
    ClassifyJobUrlInput,
    ClassifyJobUrlOutput,
    DeduplicateObservedJobsInput,
    DeduplicateObservedJobsOutput,
    ExtractObservedJobDetailsBatchInput,
    ExtractObservedJobDetailsBatchOutput,
    ExtractObservedJobDetailsInput,
    ExtractObservedJobDetailsOutput,
    FetchPublicJobPageInput,
    FetchPublicJobPageOutput,
    FetchPublicJobPagesInput,
    FetchPublicJobPagesOutput,
    FetchWechatArticleInput,
    FetchWechatArticleOutput,
    QueryCareerSheetRecordsInput,
    QueryCareerSheetRecordsOutput,
    SearchJobSiteInput,
    SearchJobSiteOutput,
    SearchPublicJobPagesInput,
    SearchPublicJobPagesOutput,
    ValidateObservedCandidatesInput,
    ValidateObservedCandidatesOutput,
)
from .business.job_matching.job_matching import (
    MatchObservedJobsInput,
    MatchObservedJobsOutput,
    match_observed_jobs,
)
from .business.resume_tailoring.resume_tailoring import (
    BuildResumeTailoringBriefInput,
    ResumeTailoringBriefOutput,
    build_resume_tailoring_brief,
)
from .business.skill_references import (
    READ_REFERENCE_SKILLS,
    ReadSkillReferenceInput,
    ReadSkillReferenceOutput,
    read_skill_reference,
)
from .context import ToolContext
from .network.batch_fetch import fetch_public_job_pages
from .network.browse import browse_public_job_page, search_job_site
from .network.career_sheets import query_career_sheet_records
from .network.classify_url import classify_job_url
from .network.page_fetch import fetch_public_job_page
from .network.public_search import search_public_job_pages
from .network.wechat import fetch_wechat_article

#: Single source of truth: tool_name -> persisted artifact_type.
#: Canonical artifact ports consumed by the kernel evidence boundary.  Adding
#: a new tool that produces a persisted artifact requires an entry here.
#: Verbatim from ``backend/app/services/career_skills/registry.py``.
TOOL_ARTIFACT_TYPE: dict[str, str] = {
    "fetch-public-job-pages": "public_job_page",
    "fetch-public-job-page": "public_job_page",
    "fetch-wechat-article": "public_job_page",
    "browse-public-job-page": "public_job_page",
    "search-job-site": "public_job_page",
    "search-public-job-pages": "job_search_results",
    "query-career-sheet-records": "job_search_results",
    "extract-observed-job-details": "structured_job_details",
    "extract-observed-job-details-batch": "structured_job_details",
    "match-observed-jobs": "job_matching_report",
    "build-resume-tailoring-brief": "resume_tailoring_brief",
    "build-preparation-plan": "career_preparation_plan",
}


@dataclass(frozen=True)
class ToolContract:
    """Model-facing execution contract used for atomic tool orchestration."""

    granularity: str = "atomic"
    max_items: int | None = None
    fallback_route: str | None = None
    preferred_for_agent: bool = True


# Keep batch/compound implementations available to the skill runtime, while
# making their orchestration cost explicit to the model.  The business handler
# remains unchanged; this is a contract layer, not a second tool registry.
TOOL_CONTRACTS: dict[str, ToolContract] = {
    "fetch-public-job-pages": ToolContract(
        granularity="batch",
        max_items=10,
        fallback_route="fetch-public-job-page",
        preferred_for_agent=False,
    ),
    "extract-observed-job-details-batch": ToolContract(
        granularity="batch",
        max_items=10,
        fallback_route="extract-observed-job-details",
        preferred_for_agent=False,
    ),
    "fetch-wechat-article": ToolContract(
        granularity="composite",
        max_items=1,
        fallback_route="fetch-public-job-page",
    ),
    "browse-public-job-page": ToolContract(
        granularity="composite",
        max_items=1,
        fallback_route="fetch-public-job-page",
    ),
    "search-job-site": ToolContract(
        granularity="source_query",
        max_items=20,
        fallback_route="search-public-job-pages",
    ),
    "search-public-job-pages": ToolContract(
        granularity="source_query",
        max_items=20,
        fallback_route="query-career-sheet-records",
    ),
    "query-career-sheet-records": ToolContract(
        granularity="source_query",
        max_items=100,
        fallback_route="search-public-job-pages",
    ),
    "match-observed-jobs": ToolContract(
        granularity="deliverable",
        max_items=100,
    ),
    "build-resume-tailoring-brief": ToolContract(
        granularity="deliverable",
        max_items=1,
    ),
    "build-preparation-plan": ToolContract(
        granularity="deliverable",
        max_items=1,
    ),
}


@dataclass(frozen=True)
class ToolDefinition:
    """One registered skill tool: model contracts + the sync handler."""

    name: str
    skill_name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], Any]
    is_deliverable: bool = False
    artifact_type: str | None = None
    # Shared infrastructure tools can be exposed to several skill agents
    # while remaining a single registered model-facing tool name.
    allowed_skills: frozenset[str] | None = None

    @property
    def contract(self) -> ToolContract:
        """Return the explicit orchestration contract for this tool."""
        return TOOL_CONTRACTS.get(self.name, ToolContract())

    @property
    def agent_description(self) -> str:
        """Description with bounded execution semantics for the model."""
        contract = self.contract
        route = f"; fallback={contract.fallback_route}" if contract.fallback_route else ""
        preferred = "优先" if contract.preferred_for_agent else "仅在批量输入时"
        return (
            f"{self.description}\n"
            f"工具契约：granularity={contract.granularity}; "
            f"{preferred}使用; max_items={contract.max_items or '1'}{route}。"
        )


class CareerToolRegistry(Mapping[str, ToolDefinition]):
    """Registered tool catalog with a deterministic invoke entry.

    Implements ``Mapping[str, ToolDefinition]`` so it can be used anywhere a
    dict catalog was expected.  ``invoke`` is the **trusted-kernel** path:
    it enforces neither skill isolation nor thread offloading (the model
    never reaches it — see ``tool_adapter.invoke_tool`` for the model-facing
    boundary with isolation + ``asyncio.to_thread``).
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    # -- registration -------------------------------------------------------

    def register(self, definition: ToolDefinition) -> None:
        """Register one tool definition (idempotent replace of same name)."""
        self._tools[definition.name] = definition

    # -- mapping protocol ---------------------------------------------------

    def __getitem__(self, name: str) -> ToolDefinition:
        return self._tools[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def get(self, name: str, default: ToolDefinition | None = None) -> ToolDefinition | None:
        return self._tools.get(name, default)

    def tool_names(self) -> frozenset[str]:
        """Return the registered tool names as a frozenset."""
        return frozenset(self._tools)

    # -- catalog ------------------------------------------------------------

    def catalog_by_skill(self) -> dict[str, list[str]]:
        """Return ``{skill_name: [tool names...]}`` in registration order."""
        catalog: dict[str, list[str]] = {}
        for definition in self._tools.values():
            skills = definition.allowed_skills or frozenset({definition.skill_name})
            for skill_name in sorted(skills):
                catalog.setdefault(skill_name, []).append(definition.name)
        return catalog

    # -- trusted-kernel invoke ----------------------------------------------

    def invoke(
        self,
        tool_name: str,
        context: ToolContext,
        params: dict[str, Any],
        tool_call_id: str | None = None,
    ) -> Any:
        """Run one tool synchronously and return a ``ToolObservation``.

        Unknown tool names produce an ``unknown_tool`` observation; handler
        failures are converted (never raised) so the kernel cannot be
        destabilized by a raw exception.  Skill isolation is deliberately NOT
        enforced here — this entry is only reachable from the trusted kernel
        (controller / deterministic fallback), which decides what it calls.
        """
        from .tool_adapter import invoke_tool_sync

        return invoke_tool_sync(
            registry=self,
            context=context,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            params=params,
        )


def build_career_tool_registry() -> CareerToolRegistry:
    """Build the reviewed catalog: 14 tools across four skills.

    Existing descriptions are verbatim from ``backend/app/services/
    career_skills/registry.py``; career-planning uses the approved archived
    Skill contract. Input/output models point at the ported pydantic models.
    """
    registry = CareerToolRegistry()
    registry.register(
        ToolDefinition(
            name="read-skill-reference",
            skill_name="skill-reference",
            allowed_skills=READ_REFERENCE_SKILLS,
            input_model=ReadSkillReferenceInput,
            output_model=ReadSkillReferenceOutput,
            handler=read_skill_reference,
            description=(
                "按当前 skill agent 的白名单读取 archived skill 的 references/*.md；"
                "禁止跨 skill、路径穿越或读取 SKILL.md/任意包文件。"
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="extract-observed-job-details-batch",
            skill_name="job-discovery",
            input_model=ExtractObservedJobDetailsBatchInput,
            output_model=ExtractObservedJobDetailsBatchOutput,
            handler=extract_observed_job_details_batch,
            is_deliverable=True,
            artifact_type=TOOL_ARTIFACT_TYPE["extract-observed-job-details-batch"],
            description=(
                "批量把已观察页面证据规范化为详细 JD；不接受模型生成的正文。"
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="fetch-public-job-pages",
            skill_name="job-discovery",
            input_model=FetchPublicJobPagesInput,
            output_model=FetchPublicJobPagesOutput,
            handler=fetch_public_job_pages,
            is_deliverable=True,
            artifact_type=TOOL_ARTIFACT_TYPE["fetch-public-job-pages"],
            description=(
                "批量抓取用户给出的有限官方 URL，返回每页可追溯正文或明确失败原因。"
                "每个成功页面带 quality=jd_complete/list_only/js_shell/empty；"
                "只有 jd_complete 才可直接进入 JD 提取与匹配，list_only 会优先展开"
                "详情页，js_shell/empty 不得冒充 JD 成功。JS 卡片列表页会自动展开："
                "列表页本身 + 前 5 个详情页正文一并返回。failures 列表中的失败仅针对"
                "列出的 URL 本身；同批其余 URL 仍应继续逐一尝试。"
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="search-public-job-pages",
            skill_name="job-discovery",
            input_model=SearchPublicJobPagesInput,
            output_model=SearchPublicJobPagesOutput,
            handler=search_public_job_pages,
            is_deliverable=True,
            artifact_type=TOOL_ARTIFACT_TYPE["search-public-job-pages"],
            description=(
                "搜索公开招聘页；仅在用户没有提供候选 URL（或全部候选 URL 均已抓取"
                "失败：fetch 错误或 dead_link 死链）、且 smartsheet 无匹配记录时用于"
                "发现直接招聘链接；部分候选失败绝不授权搜索。"
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="query-career-sheet-records",
            skill_name="job-discovery",
            input_model=QueryCareerSheetRecordsInput,
            output_model=QueryCareerSheetRecordsOutput,
            handler=query_career_sheet_records,
            is_deliverable=True,
            artifact_type=TOOL_ARTIFACT_TYPE["query-career-sheet-records"],
            description=(
                "查询招聘 smartsheet（内推/招聘链接台账）按企业/岗位/地点关键词与近 N 天"
                "过滤，返回候选招聘 URL；每条记录带 prior_metadata（公司/投递链接/"
                "内推码/更新时间）补足页面缺失字段；主证据源，无匹配记录时才用网络搜索；"
                "当 smartsheet 接口不可用或受限（error sheet_rate_limited / "
                "sheet_call_failed，如每日访问配额 400007 用尽）时，search-public-job-"
                "pages 是授权的备用数据源，应切换到公开搜索。"
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="fetch-public-job-page",
            skill_name="job-discovery",
            input_model=FetchPublicJobPageInput,
            output_model=FetchPublicJobPageOutput,
            handler=fetch_public_job_page,
            is_deliverable=True,
            artifact_type=TOOL_ARTIFACT_TYPE["fetch-public-job-page"],
            description=(
                "抓取一页公开招聘页面并生成带来源、内容哈希和质量分级的证据；"
                "quality=js_shell/empty 时不得作为完整 JD 交付。"
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="browse-public-job-page",
            skill_name="job-discovery",
            input_model=BrowsePublicJobPageInput,
            output_model=BrowsePublicJobPageOutput,
            handler=browse_public_job_page,
            is_deliverable=True,
            artifact_type=TOOL_ARTIFACT_TYPE["browse-public-job-page"],
            description=(
                "在无头浏览器中打开一个公开招聘 URL 并与页面交互，返回完整证据页"
                "（与 fetch-public-job-page 同款分类/规范化契约）外加导航信号："
                "cards_visible 可见卡片数、estimated_total_items 总数估计、"
                "pagination_pattern 站点分页方式（query/offset/path）、strategy 与 "
                "strategy_detail 实际使用的策略、pages_collected 采集页数、warning。"
                "mode=render 稳定渲染；mode=load-all 滚动+点击\"加载更多/查看全部\"；"
                "mode=paginate 按检测到的 URL 分页模式跳转 2..N 页；mode=interact "
                "点击最多 max_cards 张职位卡片并收集各自详情。当 fetch-public-job-page "
                "返回 js_shell 或 list_only、需要浏览器内交互（抽屉/翻页/加载更多）才能"
                "得到完整 JD 时使用本工具。所有模式均在公开网络安全边界内：不登录、不"
                "绕过反爬；遇到验证码/登录墙/微信验证等安全门时拒绝交互并如实失败。"
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="search-job-site",
            skill_name="job-discovery",
            input_model=SearchJobSiteInput,
            output_model=SearchJobSiteOutput,
            handler=search_job_site,
            is_deliverable=True,
            artifact_type=TOOL_ARTIFACT_TYPE["search-job-site"],
            description=(
                "在招聘站自带的站内搜索框输入关键词并触发搜索，返回搜索后的页面证据"
                "（带 artifact_id/source_url/content_hash/quality，可直接进入提取）"
                "与搜索诊断：search_ok 是否找到搜索框、pre/post_search_card_count "
                "搜索前后可见卡片数、result_indicator 结果计数文案、warning（搜索前后"
                "卡片数相同且数量多时提示疑似客户端假过滤，结果可能不完整）。与 "
                "search-public-job-pages（外部网页搜索）不同：本工具检索的是站点自身"
                "索引。当已知列表页/门户有明显搜索框、需要在站内过滤目标岗位时使用。"
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="extract-observed-job-details",
            skill_name="job-discovery",
            input_model=ExtractObservedJobDetailsInput,
            output_model=ExtractObservedJobDetailsOutput,
            handler=extract_observed_job_details,
            is_deliverable=True,
            artifact_type=TOOL_ARTIFACT_TYPE["extract-observed-job-details"],
            description="把一份已观察页面证据规范化为详细 JD。",
        )
    )
    registry.register(
        ToolDefinition(
            name="validate-observed-candidates",
            skill_name="job-discovery",
            input_model=ValidateObservedCandidatesInput,
            output_model=ValidateObservedCandidatesOutput,
            handler=validate_observed_candidates,
            description=(
                "对已观察页面证据做确定性质量校验（陈旧年份/正文过短/非 JD 文本），"
                "供 Verifier 判 PASS/REPLAN。"
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="fetch-wechat-article",
            skill_name="job-discovery",
            input_model=FetchWechatArticleInput,
            output_model=FetchWechatArticleOutput,
            handler=fetch_wechat_article,
            is_deliverable=True,
            artifact_type=TOOL_ARTIFACT_TYPE["fetch-wechat-article"],
            description=(
                "OCR 抓取微信公众号图文（含 ReadGZH 镜像）为可提取文本与候选。微信"
                "图文正文是图片，普通页面抓取返回空内容——目标为 mp.weixin.qq.com 链接"
                "时使用本工具（fetch-public-job-pages 也已自动路由微信链接）；门控关闭时"
                "返回 needs_manual_review（reason ocr_disabled）。注意：单个微信链接"
                "失败（如镜像返回验证墙/付费墙、文章无正文）只代表该链接本身不可用，不"
                "代表其他微信文章链接也会失败——每篇独立尝试，其余链接仍应继续抓取。"
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="deduplicate-observed-jobs",
            skill_name="job-discovery",
            input_model=DeduplicateObservedJobsInput,
            output_model=DeduplicateObservedJobsOutput,
            handler=deduplicate_observed_jobs,
            description=(
                "对已观察页面证据按 canonical 身份（job_id/apply_url/规范化标题）"
                "做 run 内确定性去重，返回 kept/removed。"
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="classify-job-url",
            skill_name="job-discovery",
            input_model=ClassifyJobUrlInput,
            output_model=ClassifyJobUrlOutput,
            handler=classify_job_url,
            description=(
                "对候选 URL 做低预算站点分类（wechat/adapter/static/spa/blocked，"
                "host 信号 + 4KB 探针，不启动浏览器）。"
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="match-observed-jobs",
            skill_name="job-matching",
            input_model=MatchObservedJobsInput,
            output_model=MatchObservedJobsOutput,
            handler=match_observed_jobs,
            is_deliverable=True,
            artifact_type=TOOL_ARTIFACT_TYPE["match-observed-jobs"],
            description=(
                "对已观察 JD 按已确认能力、地点和可验证待遇/公司属性做透明匹配排序；"
                "推荐任务必须调用。"
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="build-resume-tailoring-brief",
            skill_name="resume-tailoring",
            input_model=BuildResumeTailoringBriefInput,
            output_model=ResumeTailoringBriefOutput,
            handler=build_resume_tailoring_brief,
            is_deliverable=True,
            artifact_type=TOOL_ARTIFACT_TYPE["build-resume-tailoring-brief"],
            description="基于已确认简历事实与一个 JD 生成不可虚构、可审阅的简历修改建议。",
        )
    )
    registry.register(
        ToolDefinition(
            name="build-preparation-plan",
            skill_name="career-planning",
            input_model=BuildPreparationPlanInput,
            output_model=CareerPreparationPlanOutput,
            handler=build_preparation_plan,
            is_deliverable=True,
            artifact_type=TOOL_ARTIFACT_TYPE["build-preparation-plan"],
            description=(
                "基于已观察目标 JD 生成可审阅、可追溯的求职准备计划；"
                "不得捏造截止日期或执行投递。"
            ),
        )
    )
    return registry


#: Default catalog — built once at import.  ``build_career_tool_registry``
#: stays public so hermetic tests can construct registries with stub handlers.
CAREER_TOOL_REGISTRY = build_career_tool_registry()

#: Per-skill catalog — Phase 5 uses this to scope each subagent's tool grant.
#: job-discovery 11 / job-matching 2 / resume-tailoring 2 / career-planning 2.
TOOL_CATALOG_BY_SKILL: dict[str, list[str]] = (
    CAREER_TOOL_REGISTRY.catalog_by_skill()
)


__all__ = [
    "ToolContract",
    "ToolDefinition",
    "CareerToolRegistry",
    "TOOL_ARTIFACT_TYPE",
    "TOOL_CONTRACTS",
    "TOOL_CATALOG_BY_SKILL",
    "CAREER_TOOL_REGISTRY",
    "build_career_tool_registry",
]
