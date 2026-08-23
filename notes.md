# Notes: pi-py Career Agent Migration Continuation

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
