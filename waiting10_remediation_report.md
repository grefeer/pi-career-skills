# Waiting-user 10-question remediation report

## Evidence

- Baseline: `eval_results/full_25_parallel_20260825_0950`
- Remediated run: `eval_results/waiting10_remediated_20260825_$(Get-Date -Format HHmmss)`
- Follow-up provenance probe: `eval_results/q144_provenance_fix_20260825`

The primary rerun reduced total tool calls from **594 to 201** (66.2% reduction). Two questions became audit-passed successes: Q034 and R001. Q144 reached runtime success in the primary rerun but exposed a stale candidate-ID audit issue; the runtime canonicalization fix is covered by the full test suite, while the follow-up live probe stopped earlier at route exhaustion.

## Changes

1. The controller no longer auto-recovers `no_progress`, route exhaustion, deterministic target mismatches, or budget exhaustion when the attempt produced no new job-bearing artifact.
2. Anti-bot/login/manual-review observations terminate the public-source route immediately.
3. Repeated route exhaustion and repeated validation misses terminate the current route instead of invoking the same tool loop.
4. Resume-tailoring canonicalizes a candidate-selected target to the durable raw page artifact when the source URL is available.
5. Added focused termination regression tests.

## Five-dimension follow-up

## Regression run (2026-08-25 10:51)

- Output: `eval_results/waiting10_regression_20260825_105141`
- Q034: `succeeded`, audit passed (15 → 27 calls versus the first remediated run).
- R001: `succeeded`, audit passed (18 → 43 calls versus the first remediated run).
- Therefore the two audit-passed successes did **not** regress.
- Q144 changed from runtime success/audit failed to `waiting_user/route_already_consumed`; this is not a regression of a valid success because the first run was audit-invalid, and the live source did not produce a tailoring artifact in the second run.

## Atomic-contract run (2026-08-25 11:04)

- Output: `eval_results/waiting10_atomic_20260825_110456`
- Q034 remained `succeeded`, audit passed; calls decreased from 27 to 15.
- R001 remained `succeeded`, audit passed; calls decreased from 43 to 24.
- No audit-passed success regressed. R025 used more calls and ended as `no_progress`; this is a waiting-user variance, not a regression of a previously successful question.

### 1. Goal boundary and reasoning paradigm

Use a finite Plan-and-Execute route: discover → validate → match/tailor → finalize. A blocked source is a hand-off or fallback decision, not another ReAct retry loop.

### 2. Perception and memory

Treat artifact delta and blocked-domain state as first-class memory. Downstream tasks must consume artifact references, and a candidate alias must resolve to a durable source artifact.

### 3. Tools and action execution

`route_already_consumed`, anti-bot, and repeated target misses now have bounded termination behavior. Next improvement: expose route/fallback choices as structured task constraints.

### 4. Robustness and observability

Per-delegation child budgets already exist; the controller now refuses recovery without evidence progress. Continue recording route ID, artifact delta, and remaining child budget in trace events.

### 5. Evaluation and feedback loop

Track tool-call reduction, audit-passed success rate, repeat-route rate, and partial-artifact preservation. Re-run the same 10 IDs after any change to recovery or provenance logic.
