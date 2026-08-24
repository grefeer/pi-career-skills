# Notes: pi-py Career Agent Migration Continuation

## Phase 9 evaluation continuation (2026-08-24)

- Existing user-owned result evidence is in `eval_results/`; do not delete or overwrite it during investigation.
- Completed before this continuation: Phase 0–8; Layer 1 faux smoke (6 records); Layer 2 real non-chain (3 records, then Q045 rerun in progress).
- Known corrected defect: completion evidence must use ANY qualifying summary reference, not require all tail artifacts to resolve to job-bearing content. The prior regression result was `380/380`.
- Next evidence target: Q045 rerun, then C001/C003/C002 real chain records. Full 83-record run is conditional on those records being schema-valid and the local runner remaining healthy.
- Triaging rule: classify external anti-bot/site availability, expected handoffs, and budget exhaustion separately from local runner/schema/completion-gate defects.

## P0 evidence found before Layer 3/4

- `eval_results/pi_real_nonchain/Q045.json`: timestamp `09:03:16`; completion-gate fix commit `aa61c07`: `09:04:12`. No evaluation process was present at the later snapshot, so the file cannot validate the fix.
- `evaluation.cli._load_ids_from_manifest()` calls `str()` for a JSON list. The intended 83-record manifest is a list of objects such as `{"id": "Q011", "kind": "keep", "profile": "P3"}`, producing an invalid literal filename instead of `Q011.json`.
- Four-worker results are stored in `<out>/workerN/`; `evaluation.compare.merge_results()` and source loading only use a non-recursive `glob("*.json")`. The advertised comparison would otherwise silently see zero worker records.
- C003's required skill is `career-planning`. Current pi allowed-skill, factory, controller, and completion-checker sets expose only job discovery, matching, and resume tailoring. Treat this as a migration-scope compatibility decision, not a flaky live-site result.

## Q045 post-fix real run

- Input: source fixture `tests/question/redesign/Q045.json`; output: `eval_results/pi_real_nonchain_postfix/Q045.json`.
- Result: `succeeded`, `error_code=null`, `wall_seconds=106.609`, two persisted `public_job_page` artifacts with `quality=jd_complete`, and `audit.status=passed`.
- The two qualifying pages are `https://hr.ofweek.com/jobs/jobs-show-3075456.html` and `https://www.591yjs.cn/job/index.php?c=comapply&id=223`. This is a valid post-`aa61c07` confirmation of ANY summary-reference completion semantics.
- Follow-up evidence: seven fetch artifacts were promoted before multiple `extract-observed-job-details*` calls failed with `target_evidence_not_found`. This points to a potential live `EvidenceStore` → `ToolContext.metadata` projection gap. Do not assume it is external variance; reproduce with a focused handler/controller test before modifying code.

## P0 repair checkpoint

- The manifest loader now accepts the fixture's object records, and comparison recursively discovers worker subdirectories while rejecting duplicate IDs. Focused runner/comparison suites and Ruff passed before this checkpoint.
- The focused real-handler controller regression reproduced the in-run projection failure: a fetched page was promoted, but later extraction could not resolve it from the long-lived `ToolContext.metadata`. The repair refreshes bounded public-evidence and structured-candidate projections after a promoted observation while retaining private facts. Root verification: `uv run pytest packages/pi-career-skills/tests/test_controller.py packages/pi-career-skills/tests/test_career_planning_parity.py -q` → `25 passed`.
- A new deterministic `business/career_planning` adapter provides target-evidence resolution, keyword-grounded plan items, stable target errors, and relative scheduling when no date is supplied. Its focused suite reported `7 passed`; independent review is still required before integration.
- An unrelated `.gitignore` addition for `docs/` and `eval_results/` was removed. Existing user-owned documents and raw evaluation records remain visible and untouched; `.claude/` remains ignored because it may contain credentials.

## Layer 3 and baseline decisions

- Execute `C001`, `C003`, and `C002` independently in fresh timestamped directories. C001 needs a valid discovery page followed by a matching report; C003 additionally needs a planning artifact. Every succeeded link must have `audit.status=passed`.
- C002 is a valid blocked-handoff probe, not an assertion that the source always blocks. If Baidu or the fallback sources yield no qualifying JD, the target must preserve only L1 (`chain_length=1`, no L2, top-level `waiting_user`, audit `not_applicable`). A successful two-link C002 is also source-consistent. Executing L2 after an unsuccessful L1 would be an internal regression.
- For Layer 5, use source `tests/question/eval_results/full83_autorecovery_20260824` as the single `--source-nonchain` input; it has 83 unique IDs. The old worker split contributes only 58 non-chain plus 15 chain IDs (73 total), so it is not a sufficient full baseline.

## Independent-review blockers before integration

- Career planning: raw substring matching mislabels `JavaScript` as `Java`; target artifact output retains a candidate/URL selector instead of the canonical persisted artifact ID; duplicated aliases inflate skill-gap counts; and model-supplied resume skills can override confirmed facts. These are factual/provenance defects, not model variance, and must receive RED/GREEN regression fixes.
- In-run projection: the matching fallback still constructs metadata from static request context, so it misses candidates produced earlier in the same run. Existing inherited evidence can also consume all bounded context before newer store evidence is added, and alias dedup keys are too narrow. Fixes must prioritize current store evidence, use canonical source/hash deduplication, and retain the raw inherited snapshot.
- Maintainability: projection helpers pushed `runtime/controller.py` beyond its documented 800-line hard cap. Move them into a dedicated runtime module with direct tests; also clear attempt-scoped refresh callbacks through an exception-safe lifecycle.
- Context-contract audit found that controller currently prompts the goal but does not include `task_goal` in `ToolContext.metadata`; runner/chain private contexts only carry facts/evidence/candidates. Career-planning's role compatibility check would therefore silently bypass. The source injects `task_goal`; the runtime integration must do the same and add an end-to-end regression.

## Baseline

- Target repository head: `84218c9`.
- Phase 5 agent files are staged; Phase 6 network files are unstaged.
- No Phase 7 controller, model factory, or Phase 8 evaluator is present yet.

## Wave 3 verification log

- Targeted Phase 5–6 verification: `32 passed`.
- Full package verification: `244 passed` with integration tests excluded.
- Ruff: `All checks passed!`.
- Strict direct mypy run: 146 errors in 21 files. This is an existing package-wide typing baseline (including Phase 2 modules); it is not a Phase 6 test failure but prevents claiming the plan's optional strict-mypy command is green.
- Manual security review found and removed the profile-injection route from the Playwright worker. The new regression test first failed, then passed.
