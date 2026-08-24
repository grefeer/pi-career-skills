# Task Plan: Continue pi-py Career Agent Migration and 83-Question Evaluation

## Goal
Complete `docs/pi-py-agent迁移计划.md` through a verified 83-question pi-agent evaluation, repairing only failures reproduced by the layered real-run evidence.

## Phases
- [x] Recover the migration context and protect the interrupted Wave 3 changes.
- [x] Verify and review Wave 3 (Phase 5–6), repair only evidenced defects, then commit it.
- [x] Implement and verify Wave 4 / Phase 7: DeepSeek model factory and CareerRunController.
- [x] Implement and verify Wave 5 / Phase 8: schema-valid 83-question runner and comparison tooling.
- [x] Phase 9 / Layer 1: run six faux smoke records and validate schema and completion-gate behavior.
- [ ] Phase 9 / P0 runner compatibility: make the 83-record manifest loader, multi-worker result discovery, in-run evidence context, and four-skill contract match the evaluation fixtures; add focused regression tests.
- [x] Phase 9 / Layer 2: run a new Q045 record after restoring ANY summary-reference completion semantics; do not use the pre-fix result as evidence.
- [ ] Phase 9 / Layer 3: run C001 (2-link), C003 (3-link), and C002 (blocked handoff) after the fixture contract is supportable; preserve each raw result.
- [ ] Phase 9 / Layer 4: run all 83 records with four staggered workers; triage and repair only reproducible internal failures.
- [ ] Phase 9 / Layer 5: merge worker results, audit every succeeded record, flatten the source non-chain baseline, and write `comparison.md`.
- [ ] Perform final all-branch review and write the migration result report.

## Decisions
- The target repository is `../pi-py-main`; the current source repository remains read-only except for already-existing user artifacts.
- Keep all existing staged and unstaged Wave 3 work; never reset or discard it.
- Do not start Phase 7 until Phase 6 passes its targeted security and lifecycle checks.
- Preserve the existing untracked `eval_results/` directory: it contains real-run evidence and is not disposable test output.
- Treat public-site failures, model budget exhaustion, and a valid blocked handoff as evaluation outcomes, not code defects, unless a local invariant or runner failure is reproduced.
- Do not launch Layer 4 until Layers 2–3 yield valid schema records and the runner's worker isolation is confirmed.
- Run Layer 3 as isolated C001, C003, then C002 records in fresh timestamped output directories. C002 may validly succeed or may stop after L1 when the public Baidu evidence is unavailable; only an incorrect attempt to run L2 after an unsuccessful L1 is a local regression.
- Use the complete 83-ID source baseline `full83_autorecovery_20260824` for Layer 5. The historical split baseline covers only 73 unique IDs and must not be treated as a full comparison.

## Errors Encountered
- The user-provided source-repository location differs from the pi-py target repository. Corrected before implementation.
- Layer 2 exposed a completion-gate semantic regression: pi-py required every tail summary reference to resolve to a job-bearing artifact, while the source implementation succeeds when any qualifying reference resolves. The local correction restores the source ANY semantics and has a 380-test regression suite result reported by the prior run.
- Q011 previously exhausted its model budget after 34 discovery navigations. This is an evaluation behavior to measure across the full run before changing a budget policy.
- The observed Q045 file is pre-fix evidence: its timestamp (`09:03:16`) precedes completion-fix commit `aa61c07` (`09:04:12`); no post-fix Q045 process or record exists.
- Layer 4 has a deterministic manifest-loader failure: `tests/question/redesign/manifest.json` contains objects with an `id` field, while `_load_ids_from_manifest` applies `str()` to each object and builds an impossible question filename.
- Layer 4 comparison is also deterministically incomplete: worker records live below `worker0..3/`, but `compare.py` only scans the output root non-recursively.
- C003 requires `career-planning`, which is not part of the current three-skill factory/controller/completion-policy set. Its current outcome would be a local unsupported-skill handoff rather than a meaningful chain evaluation.
- Fresh post-fix Q045 succeeded (`106.609s`, two `jd_complete` artifacts, passed audit), confirming the completion gate repair. Its event trace also shows repeated `extract-observed-job-details* → target_evidence_not_found` after artifacts were promoted; investigate this real evidence/context projection boundary before accepting a chain result that depends on structured candidates.

## Status
**Currently at Phase 9 P0 compatibility checkpoint** — Q045 post-fix completion gate has passed. The in-run evidence projection and career-planning business adapter have focused GREEN tests; independent code review is pending before their runtime integration. Do not start Layer 3 until that integration is verified.

## Implementation Tasks

### Task 1: Manifest compatibility

Make the runner accept the source 83-question object manifest while retaining string-list compatibility. Add fail-closed entry validation and focused regression coverage.

### Task 2: Worker result discovery

Make pi and source comparison inputs discover worker-subdirectory records recursively. Detect duplicate record IDs rather than silently choosing a result.

### Task 3: Chain context projection

Project inherited public evidence and structured candidates into downstream `ToolContext.metadata` through `RunRequest.private_context`, with a regression test that captures the request.

### Task 4: Career-planning scope decision

Compare C003's source skill/tool/artifact/completion contract to the target. Either port the smallest complete capability needed for meaningful evaluation or record it as an explicit scope boundary before Layer 4; do not label the deterministic unsupported-skill result as external variance.

**Decision:** Port it. It affects 16 of 83 records and source baseline has successful C003 L3 records. Preserve the source evidence-grounded tool contract, but follow its published policy/eval case for a no-date request: emit relative scheduling rather than fabricate a calendar date.

### Task 5: In-run evidence context projection

After each promoted tool observation, refresh the active skill `ToolContext.metadata` with bounded observed evidence and structured candidates from `EvidenceStore`. Prove a real discovery extraction can resolve a page promoted earlier in the same delegation.

### Task 6: Career-planning business adapter

Port the deterministic evidence-grounded planning model and handler with tests for target resolution, factual topics, missing evidence, and relative versus explicit schedules.

### Task 7: Career-planning runtime integration

Register and route the planning tool, persist and validate `career_preparation_plan`, add its completion/audit contract, update prompts/resources/snapshots, and prove a C003-style planning link consumes inherited evidence.

## Execution ledger

- [x] Task 1 — object-manifest compatibility; focused runner tests and Ruff passed.
- [x] Task 2 — recursive multi-worker result discovery and duplicate-ID failure; focused comparison tests and Ruff passed.
- [x] Task 3 — downstream chain context and matching-fallback metadata propagation; focused chain/controller tests and Ruff passed.
- [~] Task 5 — in-run dynamic evidence projection; initial refactor fixed fallback/lifecycle/size issues, but final review found seed-recency, multi-candidate, alias-resolution, and complete-byte-cap defects. Additional RED tests and repair are in progress.
- [x] Task 6 — career-planning business adapter; final independent review approved the evidence-bound model, canonical pointer, alias logic, fact-authority policy, zero-hour guard, and stable `CareerToolError` propagation (`22` focused tests).
- [x] Task 7A — registry/agent/docs/snapshot integration independently approved (13 tools / 4 skills).
- [~] Task 7C — evaluation seed/audit/chain integration has focused GREEN tests and awaits independent review.
- [ ] Task 7B — runtime evidence/completion/controller integration remains blocked on Task 5's final projection review, because both touch controller/test_controller.
- [x] Removed an unrelated `.gitignore` change that would have hidden `docs/` and `eval_results/`; retained the existing `.claude/` sensitive-settings rule.
