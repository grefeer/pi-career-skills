# Task Plan: Continue pi-py Career Agent Migration

## Goal
Complete `docs/pi-py-agent迁移计划.md` through a verified 83-question pi-agent evaluation.

## Phases
- [x] Recover the migration context and protect the interrupted Wave 3 changes.
- [ ] Verify and review Wave 3 (Phase 5–6), repair only evidenced defects, then commit it. (Verification and review complete; awaiting commit confirmation.)
- [ ] Implement and verify Wave 4 / Phase 7: DeepSeek model factory and CareerRunController.
- [ ] Implement and verify Wave 5 / Phase 8: schema-valid 83-question runner and comparison tooling.
- [ ] Run Phase 9 layered evaluations, including the full 83-question run when credentials and public-network conditions allow.
- [ ] Perform final review and write the migration result report.

## Decisions
- The target repository is `../pi-py-main`; the current source repository remains read-only except for already-existing user artifacts.
- Keep all existing staged and unstaged Wave 3 work; never reset or discard it.
- Do not start Phase 7 until Phase 6 passes its targeted security and lifecycle checks.

## Errors Encountered
- The user-provided source-repository location differs from the pi-py target repository. Corrected before implementation.

## Status
**Currently at the Wave 3 commit checkpoint** — 244 package tests and Ruff pass; `task-H-report.md` records the manual review and the separate strict-mypy debt.
