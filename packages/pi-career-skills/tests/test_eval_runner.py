"""Evaluation runner tests — hermetic via faux provider + stub registry.

Covers: end-to-end run_question, atomic write, fail-closed ids, CLI,
wall_seconds, config.seeded_urls, and private_context reaching delegation.
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
from pi_career_skills.evaluation.runner import (
    ALL_SKILLS,
    run_question,
)
from pi_career_skills.evaluation.schema import validate_record
from pi_career_skills.evaluation.seed_urls import resolve_seed_urls
from pi_career_skills.registry import ToolDefinition
from pi_career_skills.runtime.controller import CareerRunController

# ======================================================================
# Helpers — same stub-registry pattern as test_controller.py
# ======================================================================


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class StubHandler:
    """Recordable stub handler wrapper."""

    def __init__(self, fn: Any, counter: dict[str, int], name: str) -> None:
        self._fn = fn
        self._counter = counter
        self._name = name

    def __call__(self, ctx: Any, params: Any) -> Any:
        self._counter[self._name] = self._counter.get(self._name, 0) + 1
        # Store the last context for private_context assertions.
        self._last_ctx = ctx
        return self._fn(ctx, params)


def build_stub_registry() -> tuple[Any, dict[str, int], dict[str, Any]]:
    """Build a stub registry + call-count dict + last-context dict."""
    counts: dict[str, int] = {}
    last_contexts: dict[str, Any] = {}

    def _wrap(name: str, fn: Any) -> Any:
        handler = StubHandler(fn, counts, name)
        last_contexts[name] = handler
        return handler

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
    tools["search-public-job-pages"] = ToolDefinition(
        name="search-public-job-pages",
        skill_name="job-discovery",
        input_model=SearchPublicJobPagesInput,
        output_model=SearchPublicJobPagesOutput,
        handler=_wrap("search-public-job-pages", _empty_search),
        artifact_type="job_search_results",
        description="stub search",
    )
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
            from pi_career_skills.tool_adapter import invoke_tool_sync

            return invoke_tool_sync(
                registry=self,
                context=context,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                params=params,
            )

    return _StubRegistry(tools), counts, last_contexts


# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture(autouse=True)
def _clear_faux() -> None:
    clear_scripts()
    yield
    clear_scripts()


@pytest.fixture
def stub_registry() -> tuple[Any, dict[str, int], dict[str, Any]]:
    return build_stub_registry()


@pytest.fixture
def controller_factory(
    stub_registry: tuple[Any, dict[str, int], dict[str, Any]],
) -> Any:
    reg, _counts, _ctxs = stub_registry

    def _factory() -> CareerRunController:
        return CareerRunController(
            FAUX_MODEL,
            registry=reg,
            get_api_key=lambda provider: "test-key",
        )

    return _factory


def _make_question_file(
    tmp_path: Path,
    qid: str,
    *,
    question: str = "帮我找Java后端岗位",
    skills: list[str] | None = None,
    profile: dict | None = None,
) -> None:
    """Write a question JSON file into tmp_path."""
    doc = {
        "id": qid,
        "question": question,
        "meta": {"skills": skills or ["job-discovery"]},
        "profile": profile or {
            "role": "Java 后端开发工程师",
            "summary": "技能：Java, Spring Boot，社招（3 年经验）",
            "resume_text": None,
        },
    }
    (tmp_path / f"{qid}.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ======================================================================
# 1. End-to-end run_question success
# ======================================================================


@pytest.mark.asyncio
async def test_run_question_succeeds_writes_valid_json(
    tmp_path: Path,
    controller_factory: Any,
    stub_registry: tuple[Any, dict[str, int], dict[str, Any]],
) -> None:
    _reg, counts, _ctxs = stub_registry
    qid = "Q011"
    _make_question_file(tmp_path, qid)
    out_dir = tmp_path / "out"

    # Script: supervisor delegates discovery → fetch + extract → done
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
    push_script(FauxScript(text="已为您找到Java后端岗位。"))

    record = await run_question(
        qid,
        question_dir=tmp_path,
        out_dir=out_dir,
        model_id="faux",
        controller_factory=controller_factory,
    )

    # Record validates against schema.
    validate_record(record)
    assert record["type"] == "single"
    assert record["id"] == qid
    assert record["result"]["status"] == "succeeded"
    assert record["wall_seconds"] >= 0

    # File written.
    out_file = out_dir / f"{qid}.json"
    assert out_file.exists()
    loaded = json.loads(out_file.read_text(encoding="utf-8"))
    assert loaded["id"] == qid
    assert loaded["result"]["status"] == "succeeded"

    # No .tmp file left behind.
    assert not (out_dir / f"{qid}.json.tmp").exists()

    # seeded_urls populated.
    seeded_urls, _ = resolve_seed_urls(qid)
    assert record["config"]["seeded_urls"] == seeded_urls

    # Artifacts present.
    assert len(record["artifacts"]) > 0
    assert any(a["artifact_type"] == "public_job_page" for a in record["artifacts"])

    # Tool invocations happened (at least fetch was called).
    assert counts.get("fetch-public-job-pages", 0) >= 1


# ======================================================================
# 2. Atomic write — no .tmp leftover
# ======================================================================


@pytest.mark.asyncio
async def test_atomic_write_no_tmp_leftover(
    tmp_path: Path, controller_factory: Any
) -> None:
    qid = "Q011"
    _make_question_file(tmp_path, qid)
    out_dir = tmp_path / "out"

    push_script(FauxScript(text="直接返回文本。"))

    await run_question(
        qid,
        question_dir=tmp_path,
        out_dir=out_dir,
        model_id="faux",
        controller_factory=controller_factory,
    )

    json_files = list(out_dir.glob("*.json"))
    assert len(json_files) == 1
    assert json_files[0].name == f"{qid}.json"
    tmp_files = list(out_dir.glob("*.tmp"))
    assert len(tmp_files) == 0


# ======================================================================
# 3. Fail-closed: missing id
# ======================================================================


def test_cli_missing_id_fails_closed(tmp_path: Path) -> None:
    from pi_career_skills.evaluation.cli import _validate_ids

    q_dir = tmp_path / "questions"
    q_dir.mkdir()
    _make_question_file(q_dir, "Q011")

    # Missing id should raise SystemExit with code 1.
    with pytest.raises(SystemExit) as exc_info:
        _validate_ids(["Q011", "Q999"], q_dir)
    assert exc_info.value.code == 1


# ======================================================================
# 4. Fail-closed: duplicate id
# ======================================================================


def test_cli_duplicate_id_fails_closed(tmp_path: Path) -> None:
    from pi_career_skills.evaluation.cli import _validate_ids

    q_dir = tmp_path / "questions"
    q_dir.mkdir()
    _make_question_file(q_dir, "Q011")

    with pytest.raises(SystemExit) as exc_info:
        _validate_ids(["Q011", "Q011"], q_dir)
    assert exc_info.value.code == 1


# ======================================================================
# 5. Manifest id formats and fail-closed validation
# ======================================================================


@pytest.mark.parametrize(
    "payload",
    [
        [{"id": "Q011"}, {"id": "C001"}, {"id": "R002"}],
        {"ids": [{"id": "Q011"}, {"id": "C001"}, {"id": "R002"}]},
    ],
)
def test_load_ids_from_manifest_accepts_object_entries(
    tmp_path: Path, payload: Any
) -> None:
    from pi_career_skills.evaluation.cli import _load_ids_from_manifest

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    assert _load_ids_from_manifest(manifest_path) == ["Q011", "C001", "R002"]


@pytest.mark.parametrize(
    "payload",
    [
        ["Q011", "C001", "R002"],
        {"ids": ["Q011", "C001", "R002"]},
    ],
)
def test_load_ids_from_manifest_accepts_legacy_string_entries(
    tmp_path: Path, payload: Any
) -> None:
    from pi_career_skills.evaluation.cli import _load_ids_from_manifest

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    assert _load_ids_from_manifest(manifest_path) == ["Q011", "C001", "R002"]


@pytest.mark.parametrize(
    "entries",
    [
        [{"id": "Q011"}, "C001"],
        ["Q011", {"id": "C001"}],
    ],
)
def test_load_ids_from_manifest_rejects_mixed_entry_formats(
    tmp_path: Path, entries: list[Any]
) -> None:
    from pi_career_skills.evaluation.cli import _load_ids_from_manifest

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(entries), encoding="utf-8")

    with pytest.raises(ValueError, match="Cannot extract ids from manifest"):
        _load_ids_from_manifest(manifest_path)


@pytest.mark.parametrize(
    "entries",
    [
        [{"kind": "keep"}],
        [{"id": ""}],
        [{"id": "   "}],
        [{"id": 11}],
        [None],
        [11],
        {"ids": "Q011"},
    ],
)
def test_load_ids_from_manifest_rejects_invalid_entries(
    tmp_path: Path, entries: list[Any]
) -> None:
    from pi_career_skills.evaluation.cli import _load_ids_from_manifest

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(entries), encoding="utf-8")

    with pytest.raises(ValueError, match="Cannot extract ids from manifest"):
        _load_ids_from_manifest(manifest_path)


# ======================================================================
# 6. wall_seconds >= 0
# ======================================================================


@pytest.mark.asyncio
async def test_wall_seconds_non_negative(
    tmp_path: Path, controller_factory: Any
) -> None:
    qid = "Q011"
    _make_question_file(tmp_path, qid)
    out_dir = tmp_path / "out"

    push_script(FauxScript(text="hello"))

    record = await run_question(
        qid,
        question_dir=tmp_path,
        out_dir=out_dir,
        model_id="faux",
        controller_factory=controller_factory,
    )

    assert record["wall_seconds"] >= 0


# ======================================================================
# 6. config.seeded_urls populated from seed_urls
# ======================================================================


@pytest.mark.asyncio
async def test_config_seeded_urls_populated(
    tmp_path: Path, controller_factory: Any
) -> None:
    qid = "Q011"
    _make_question_file(tmp_path, qid)
    out_dir = tmp_path / "out"

    push_script(FauxScript(text="done"))

    record = await run_question(
        qid,
        question_dir=tmp_path,
        out_dir=out_dir,
        model_id="faux",
        controller_factory=controller_factory,
    )

    expected_seeds, _ = resolve_seed_urls(qid)
    assert record["config"]["seeded_urls"] == expected_seeds
    assert isinstance(record["config"]["seeded_urls"], list)


# ======================================================================
# 7. private_context reaches delegation ToolContext
# ======================================================================


@pytest.mark.asyncio
async def test_private_context_reaches_delegation(
    tmp_path: Path,
    controller_factory: Any,
    stub_registry: tuple[Any, dict[str, int], dict[str, Any]],
) -> None:
    _reg, _counts, last_contexts = stub_registry
    qid = "Q011"
    _make_question_file(tmp_path, qid)
    out_dir = tmp_path / "out"

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
    push_script(FauxScript(text="完成。"))
    push_script(FauxScript(text="总结。"))

    await run_question(
        qid,
        question_dir=tmp_path,
        out_dir=out_dir,
        model_id="faux",
        controller_factory=controller_factory,
    )

    # Check that the fetch handler received a context with confirmed_profile_facts.
    fetch_handler = last_contexts["fetch-public-job-pages"]
    ctx = fetch_handler._last_ctx
    assert ctx is not None
    assert hasattr(ctx, "metadata")
    assert "confirmed_profile_facts" in ctx.metadata
    assert isinstance(ctx.metadata["confirmed_profile_facts"], dict)
    assert len(ctx.metadata["confirmed_profile_facts"]) > 0


# ======================================================================
# 8. CLI --ids with faux runs end-to-end
# ======================================================================


def test_cli_ids_faux_runs_and_writes_record(
    tmp_path: Path,
    controller_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qid = "Q011"
    q_dir = tmp_path / "questions"
    q_dir.mkdir()
    _make_question_file(q_dir, qid)
    out_dir = tmp_path / "out"

    # Push scripts for the single run.
    push_script(FauxScript(text="hello"))

    from pi_career_skills.evaluation import cli as cli_mod

    # Patch _run_in_process to use our stub controller factory.
    async def _patched_run(ids, question_dir, out_dir, model_id):
        from pi_career_skills.evaluation.runner import run_question

        for q in ids:
            await run_question(
                q,
                question_dir=question_dir,
                out_dir=out_dir,
                model_id=model_id,
                controller_factory=controller_factory,
            )
        return 0

    monkeypatch.setattr(cli_mod, "_run_in_process", _patched_run)

    exit_code = cli_mod.run_cli(
        [
            "--ids", qid,
            "--question-dir", str(q_dir),
            "--out-dir", str(out_dir),
            "--model", "faux",
        ]
    )

    assert exit_code == 0
    assert (out_dir / f"{qid}.json").exists()
    loaded = json.loads((out_dir / f"{qid}.json").read_text(encoding="utf-8"))
    assert loaded["id"] == qid


# ======================================================================
# 9. ALL_SKILLS re-exported from seed_urls
# ======================================================================


def test_all_skills_reexported() -> None:
    from pi_career_skills.evaluation.seed_urls import ALL_SKILLS as SEED_ALL

    assert ALL_SKILLS == SEED_ALL
    assert len(ALL_SKILLS) == 4
    assert "job-discovery" in ALL_SKILLS
    assert "job-matching" in ALL_SKILLS
    assert "resume-tailoring" in ALL_SKILLS


# ======================================================================
# 10. Work dirs created
# ======================================================================


@pytest.mark.asyncio
async def test_work_dirs_created(
    tmp_path: Path, controller_factory: Any
) -> None:
    qid = "Q011"
    _make_question_file(tmp_path, qid)
    out_dir = tmp_path / "out"

    push_script(FauxScript(text="done"))

    await run_question(
        qid,
        question_dir=tmp_path,
        out_dir=out_dir,
        model_id="faux",
        controller_factory=controller_factory,
    )

    work_root = out_dir / "_work" / qid
    assert work_root.exists()
    # One attempt subdirectory.
    children = list(work_root.iterdir())
    assert len(children) == 1
    assert children[0].is_dir()
