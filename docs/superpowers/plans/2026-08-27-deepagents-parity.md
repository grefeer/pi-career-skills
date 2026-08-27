# Deepagents Career Skills Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `deepagents_packages` behaviorally compatible with the existing `pi-career-skills` harness while reusing its business and network layers.

**Architecture:** Keep `HarnessState` as the deepagents trusted kernel and add a small context bridge around `RuntimeContextProjection`. Skill tools will refresh bounded evidence metadata immediately before invocation, using the current delegation goal recorded by middleware. The deepagents evaluation adapter will emit the existing `pi_eval_record_v1` contract and reuse framework-independent audit/compare code.

**Tech Stack:** Python 3.11+, deepagents 0.6.12+, LangChain AgentMiddleware, LangGraph, Pydantic v2, pytest/pytest-asyncio, uv.

**Spec:** `docs/superpowers/specs/2026-08-27-deepagents-parity-design.md`

## Global Constraints

- Do not duplicate `pi_career_skills.business`, `pi_career_skills.network`, or archived skill resources.
- Preserve bounded private context and model-visible tool output semantics from `pi_career_skills.tool_adapter`.
- Use `RuntimeContextProjection` for inherited/live evidence; do not expose the mutable `EvidenceStore` directly to model messages.
- Keep supervisor business-tool access empty and keep each subagent scoped to `TOOL_CATALOG_BY_SKILL`.
- Use TDD for every production behavior change and run the focused test after each implementation step.
- Do not include `__pycache__`, generated evaluation output, or temporary logs in package artifacts.

---

### Task 1: Add live context projection bridge

**Files:**
- Create: `deepagents_packages/src/deepagents_skills/context_bridge.py`
- Modify: `deepagents_packages/src/deepagents_skills/run_state.py`
- Modify: `deepagents_packages/src/deepagents_skills/tools_adapter.py:66-109`
- Modify: `deepagents_packages/src/deepagents_skills/skills.py:112-190`
- Test: `deepagents_packages/tests/test_context_bridge.py`

**Interfaces:**
- `ContextProjectionBridge(private_context: dict[str, Any] | None)` wraps `RuntimeContextProjection`.
- `ContextProjectionBridge.metadata(store: EvidenceStore, base: dict[str, Any], *, skill_name: str, task_goal: str | None) -> dict[str, Any]` returns a bounded copy containing live evidence, candidates, task goal, and governor defaults.
- `HarnessState.context_bridge` stores the bridge; `HarnessState.delegation_goals: dict[str, str]` stores current task descriptions.
- `CareerLangchainTool` refreshes a copied `ToolContext` immediately before `invoke_tool_sync`.

- [ ] **Step 1: Write the failing tests**

```python
def test_seeded_evidence_is_projected_into_matching_tool_context():
    state = make_state_with_seeded_jd()
    tool = make_matching_tool(state)
    tool._run(target_artifact_id="seed-page")
    assert tool.last_context.metadata["observed_public_evidence"]
    assert tool.last_context.metadata["structured_job_candidates"]

def test_projection_refreshes_after_discovery_observation():
    state = make_empty_state()
    tool = make_discovery_tool(state)
    run_fetch_and_promote(tool, state)
    assert state.projected_metadata("job-matching")["observed_public_evidence"]

def test_projection_keeps_task_goal_and_governor_defaults():
    state = make_state_with_goal("针对北京岗位生成匹配结果")
    metadata = state.projected_metadata("job-matching")
    assert metadata["task_goal"] == "针对北京岗位生成匹配结果"
    assert metadata["enforce_public_request_governor"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest deepagents_packages/tests/test_context_bridge.py -q`

Expected: FAIL because `HarnessState` has no projection bridge and the tool context remains empty after seeding.

- [ ] **Step 3: Implement the minimal bridge**

```python
class ContextProjectionBridge:
    def __init__(self, private_context):
        self._projection = RuntimeContextProjection(private_context)

    def metadata(self, store, *, skill_name, task_goal=None):
        metadata = self._projection.initial_metadata(store)
        metadata.update({
            "task_goal": task_goal,
            "enforce_public_request_governor": True,
            "public_request_interval_seconds": 2.5,
            "public_page_cache_ttl_seconds": 6 * 60 * 60,
            "public_block_cooldown_seconds": 30 * 60,
        })
        return metadata
```

  Add `HarnessState.projected_metadata(skill_name, task_goal=None)` and have `CareerLangchainTool` use `dataclasses.replace` to invoke with a fresh metadata copy. Record the current task goal in `EvidenceMiddleware` when handling supervisor `task` calls.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `uv run pytest deepagents_packages/tests/test_context_bridge.py -q`

Expected: all projection tests pass.

- [ ] **Step 5: Commit**

```powershell
git add deepagents_packages/src/deepagents_skills/context_bridge.py deepagents_packages/src/deepagents_skills/run_state.py deepagents_packages/src/deepagents_skills/tools_adapter.py deepagents_packages/src/deepagents_skills/skills.py deepagents_packages/tests/test_context_bridge.py
git commit -m "feat: project live evidence into deepagents tools"
```

### Task 2: Restore tool isolation and middleware termination semantics

**Files:**
- Modify: `deepagents_packages/src/deepagents_skills/tools_adapter.py:66-109`
- Modify: `deepagents_packages/src/deepagents_skills/middleware/harness.py:531-727`
- Test: `deepagents_packages/tests/test_tool_adapter.py`
- Test: `deepagents_packages/tests/test_middleware.py`

**Interfaces:**
- Add a private `_is_skill_allowed(context, definition)` equivalent to the pi adapter.
- `CareerLangchainTool._run` returns a `TOOL_SKILL_FORBIDDEN` envelope for a mismatched context.
- `EvidenceMiddleware.awrap_tool_call` returns a terminating `Command` when `_update_external_failures` signals a hard handoff.

- [ ] **Step 1: Write failing isolation and termination tests**

```python
def test_langchain_tool_rechecks_skill_isolation():
    tool = make_tool("job-matching", context_skill="job-discovery")
    payload = json.loads(tool._run())
    assert payload["error_code"] == "tool_skill_forbidden"

async def test_repeated_blocked_source_terminates_active_delegation():
    middleware, state = make_skill_middleware("job-discovery")
    result = await invoke_two_blocked_source_calls(middleware, state)
    assert result.update["jump_to"] == "end"
    assert state.halt_code == "anti_bot_challenge"
```

- [ ] **Step 2: Run focused tests and confirm expected failures**

Run: `uv run pytest deepagents_packages/tests/test_tool_adapter.py deepagents_packages/tests/test_middleware.py -q`

Expected: isolation currently executes through `invoke_tool_sync`; external failure currently records a halt but does not terminate the active loop.

- [ ] **Step 3: Implement minimal fixes**

Before trusted invocation, reject a context whose `skill_name` is not the definition skill. In `_handle_business_result`, after `_update_external_failures`, return a `Command(update={"jump_to": "end", "messages": [result]})` when termination is requested, while retaining the observation event and halt code.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest deepagents_packages/tests/test_tool_adapter.py deepagents_packages/tests/test_middleware.py -q`

Expected: all focused tests pass.

- [ ] **Step 5: Commit**

```powershell
git add deepagents_packages/src/deepagents_skills/tools_adapter.py deepagents_packages/src/deepagents_skills/middleware/harness.py deepagents_packages/tests/test_tool_adapter.py deepagents_packages/tests/test_middleware.py
git commit -m "fix: preserve deepagents isolation and handoff termination"
```

### Task 3: Restore capability metadata, child budgets, and model routing

**Files:**
- Modify: `deepagents_packages/src/deepagents_skills/contracts.py:27-54`
- Modify: `deepagents_packages/src/deepagents_skills/run_state.py:36-90`
- Modify: `deepagents_packages/src/deepagents_skills/controller.py:70-167`
- Modify: `deepagents_packages/src/deepagents_skills/skills.py:48-190`
- Modify: `deepagents_packages/src/deepagents_skills/middleware/harness.py:364-408`
- Test: `deepagents_packages/tests/test_capability_routing.py`

**Interfaces:**
- `CareerRunController(..., models: dict[str, BaseChatModel] | None = None)` mirrors the pi controller.
- `_child_budget_limits(skill: str) -> BudgetLimits` reads `CAPABILITY_REGISTRY`.
- `HarnessState.child_trackers: dict[str, BudgetTracker]` records bounded per-skill trackers while the run tracker remains cumulative.
- `build_supervisor_graph(..., models: Mapping[str, BaseChatModel] | None = None)` selects the configured model for each subagent.

- [ ] **Step 1: Write failing capability tests**

```python
def test_subagent_uses_capability_description_and_catalog():
    spec = make_subagent_spec("job-matching")
    assert spec["description"] == CAPABILITY_REGISTRY["job-matching"].description
    assert {tool.name for tool in spec["tools"]} == set(
        CAPABILITY_REGISTRY["job-matching"].tool_names
    )

def test_skill_budget_is_child_scoped():
    controller = make_controller()
    state = make_state()
    controller.prepare_skill_tracker(state, "job-matching")
    assert state.child_trackers["job-matching"].limits.model_requests == 16

def test_configured_skill_model_is_used():
    spec = make_subagent_spec("job-matching", models={"job-matching": matching_model})
    assert spec["model"] is matching_model
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `uv run pytest deepagents_packages/tests/test_capability_routing.py -q`

Expected: FAIL because descriptions are hard-coded, no child tracker exists, and the controller accepts only one model.

- [ ] **Step 3: Implement capability-driven routing**

Import `CAPABILITY_REGISTRY`, derive descriptions and tool names from it, add the optional `models` mapping, create a child tracker from each capability's `default_budget`, and make `BudgetMiddleware` charge the tracker selected by `skill_name` while preserving cumulative run accounting.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest deepagents_packages/tests/test_capability_routing.py -q`

Expected: all capability tests pass.

- [ ] **Step 5: Commit**

```powershell
git add deepagents_packages/src/deepagents_skills/contracts.py deepagents_packages/src/deepagents_skills/run_state.py deepagents_packages/src/deepagents_skills/controller.py deepagents_packages/src/deepagents_skills/skills.py deepagents_packages/src/deepagents_skills/middleware/harness.py deepagents_packages/tests/test_capability_routing.py
git commit -m "feat: restore capability budgets and model routing"
```

### Task 4: Fix chain seed preservation

**Files:**
- Modify: `deepagents_packages/eval_25_deepagents.py:139-175`
- Test: `deepagents_packages/tests/test_chain_seed.py`

**Interfaces:**
- `_build_seed_artifacts(prev_result: RunResult) -> list[Artifact]` returns only seedable `public_job_page` and `structured_job_details` artifacts, retaining source URL/hash and bounded content.

- [ ] **Step 1: Write failing seed tests**

```python
def test_non_structured_deliverable_becomes_public_page_seed():
    result = result_with_artifact("career_preparation_plan", {"summary": "准备计划"})
    seeds = _build_seed_artifacts(result)
    assert seeds[0].artifact_type == "public_job_page"
    assert seeds[0].content["visible_text"] == "准备计划"

def test_structured_candidates_remain_structured_seeds():
    result = result_with_structured_candidates()
    seeds = _build_seed_artifacts(result)
    assert seeds[0].artifact_type == "structured_job_details"
    assert seeds[0].tool_name == "extract-observed-job-details-batch"
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest deepagents_packages/tests/test_chain_seed.py -q`

Expected: FAIL because non-structured artifacts retain `career_preparation_plan` or `job_matching_report` as their type.

- [ ] **Step 3: Implement seed normalization**

Set `artifact_type="public_job_page"` for every non-structured seed and keep `tool_name="fetch-public-job-pages"`; skip empty content instead of creating an unusable seed.

- [ ] **Step 4: Run focused test**

Run: `uv run pytest deepagents_packages/tests/test_chain_seed.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add deepagents_packages/eval_25_deepagents.py deepagents_packages/tests/test_chain_seed.py
git commit -m "fix: preserve chain seed artifact types"
```

### Task 5: Add evaluation record parity

**Files:**
- Create: `deepagents_packages/src/deepagents_skills/evaluation/__init__.py`
- Create: `deepagents_packages/src/deepagents_skills/evaluation/schema.py`
- Create: `deepagents_packages/src/deepagents_skills/evaluation/audit.py`
- Create: `deepagents_packages/src/deepagents_skills/evaluation/runner.py`
- Create: `deepagents_packages/src/deepagents_skills/evaluation/chain.py`
- Create: `deepagents_packages/src/deepagents_skills/evaluation/compare.py`
- Create: `deepagents_packages/src/deepagents_skills/evaluation/cli.py`
- Test: `deepagents_packages/tests/test_evaluation_parity.py`

**Interfaces:**
- Re-export framework-independent `validate_record`, `audit_record`, `audit_chain`, and comparison helpers from `pi_career_skills.evaluation` where compatible.
- `default_controller_factory(model_id: str) -> Callable[[], CareerRunController]` returns a deepagents controller factory.
- `run_question(...) -> dict[str, Any]` returns a schema-valid single or chain record.
- `run_chain(...) -> dict[str, Any]` carries bounded inherited evidence through `seed_artifacts` and validates the final chain record.

- [ ] **Step 1: Write failing evaluation tests**

```python
def test_deepagents_single_record_matches_pi_eval_schema(tmp_path):
    record = run_deepagents_question("Q055", model_id="faux", out_dir=tmp_path)
    validate_record(record)
    assert record["schema_version"] == "pi_eval_record_v1"
    assert record["runtime"]["name"] == "deepagents"
    assert "audit" in record

def test_deepagents_chain_record_validates_and_has_audit(tmp_path):
    record = run_deepagents_chain("C003", model_id="faux", out_dir=tmp_path)
    validate_record(record)
    assert record["type"] == "chain"
    assert record["chain_length"] == len(record["links"])
    assert "audit" in record
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `uv run pytest deepagents_packages/tests/test_evaluation_parity.py -q`

Expected: FAIL because the current top-level runner emits only a reduced ad hoc record and has no schema/audit modules.

- [ ] **Step 3: Implement shared-schema adapters**

Build the complete `question`, `meta`, `runtime`, `model`, `config`, `result`, `attempts`, `artifacts`, `events`, `budget`, `audit`, and `wall_seconds` fields from `RunResult`. Use the existing audit functions on the generated record, call `validate_record` before writing, and write through a temporary file followed by `os.replace`.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest deepagents_packages/tests/test_evaluation_parity.py -q`

Expected: all single and chain records validate.

- [ ] **Step 5: Commit**

```powershell
git add deepagents_packages/src/deepagents_skills/evaluation deepagents_packages/tests/test_evaluation_parity.py
git commit -m "feat: add deepagents evaluation record parity"
```

### Task 6: Package integration and test coverage

**Files:**
- Modify: `pyproject.toml:36-52`
- Modify: `deepagents_packages/pyproject.toml:7-25`
- Modify: `deepagents_packages/README.md`
- Modify: `deepagents_packages/MIGRATION.md`
- Create: `deepagents_packages/tests/test_smoke.py`
- Create: `deepagents_packages/tests/test_package_layout.py`

**Interfaces:**
- The root uv workspace includes `deepagents_packages` through an explicit local project entry or an equivalent workspace source.
- The deepagents package resolves `pi-py-career-skills` from the repository rather than PyPI.
- `test_smoke.py` runs supervisor → task → skill → evidence → completion with `ScriptedFakeChatModel`.

- [ ] **Step 1: Write failing packaging/smoke tests**

```python
def test_deepagents_project_is_resolvable_from_repo():
    result = subprocess.run(["uv", "tree", "--directory", "deepagents_packages"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

async def test_faux_supervisor_skill_pipeline():
    result = await run_faux_career_task()
    assert result.events
    assert any(event.type == "tool_observation" for event in result.events)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest deepagents_packages/tests/test_smoke.py deepagents_packages/tests/test_package_layout.py -q`

Expected: FAIL because the deepagents project is outside the root workspace and the deep package currently cannot resolve `pi-py-career-skills` from the repository.

- [ ] **Step 3: Implement package integration**

Add the deepagents project to the workspace configuration without changing existing package resolution, add an explicit local source for `pi-py-career-skills`, exclude generated caches from package discovery, update README/MIGRATION counts and file layout, and make the smoke test use the current public controller API.

- [ ] **Step 4: Run package tests**

Run: `uv run pytest deepagents_packages/tests/test_smoke.py deepagents_packages/tests/test_package_layout.py -q`

Expected: PASS and `uv tree --directory deepagents_packages` exits 0.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml deepagents_packages/pyproject.toml deepagents_packages/README.md deepagents_packages/MIGRATION.md deepagents_packages/tests
git commit -m "chore: integrate and test deepagents package"
```

### Task 7: Full verification and cleanup

**Files:**
- Modify: `deepagents_packages/eval_25_deepagents.py` only if the parity runner requires a compatibility shim.
- Modify: `deepagents_packages/main_deepagents.py` only if log naming/documentation remains inconsistent.

- [ ] **Step 1: Run focused deepagents suite**

Run: `uv run pytest deepagents_packages/tests -q`

Expected: all deepagents tests pass with zero failures.

- [ ] **Step 2: Run existing career-skills suite**

Run: `uv run pytest packages/pi-career-skills/tests -q -m "not integration"`

Expected: existing suite remains green.

- [ ] **Step 3: Run lint and import checks**

Run: `uv run ruff check deepagents_packages packages/pi-career-skills`

Expected: no lint errors.

- [ ] **Step 4: Run deterministic evaluation smoke**

Run: `.venv/Scripts/python.exe deepagents_packages/eval_25_deepagents.py --model faux --qids Q055 --out temp/eval_da_final`

Expected: output JSON validates as `pi_eval_record_v1` and contains audit data.

- [ ] **Step 5: Inspect final VCS state**

Run: `git status --short --untracked-files=all`

Expected: only intentional source/test/docs changes remain; no generated caches or temporary evaluation records are staged.

- [ ] **Step 6: Commit final cleanup**

```powershell
git add deepagents_packages docs/superpowers/plans/2026-08-27-deepagents-parity.md
git commit -m "test: verify deepagents career skills parity"
```
