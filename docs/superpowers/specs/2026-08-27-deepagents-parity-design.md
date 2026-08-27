# Deepagents Career Skills Parity Design

## Goal

Bring `deepagents_packages` to behavioral parity with the existing
`pi-career-skills` harness while keeping `pi_career_skills` as the shared
business, network, security, evidence, and deterministic-tool implementation.

## Scope

The work is limited to the deepagents adapter and its integration surface.
It covers live evidence propagation, delegation context, middleware behavior,
capability/budget/model routing, evaluation records, tests, and packaging.
It does not duplicate `business/**`, `network/**`, or the archived skill
resources.

## Architecture

`HarnessState` remains the trusted per-run kernel and owns the shared
`EvidenceStore`, budget trackers, guard, and event log. A deepagents-specific
context bridge will wrap the existing `RuntimeContextProjection`: each skill
tool will build or refresh a bounded `ToolContext.metadata` view immediately
before invocation, so seeded and newly promoted evidence are visible without
sharing mutable full model state.

The supervisor continues to use deepagents `task` delegation and each skill
continues to receive only its catalog. Delegation middleware records the
current skill/task goal in `HarnessState`; the tool adapter combines that goal
with the current projection. Capability metadata will come from the existing
`CAPABILITY_REGISTRY`, while deepagents middleware will use per-skill child
budget trackers and optional per-skill models.

Evaluation will expose the same `pi_eval_record_v1` shape as the pi runner.
The deepagents runner/chain will generate complete records and reuse the
existing schema/audit/compare logic where it is framework-independent.

## Required behavior

1. Seeded artifacts and artifacts promoted during a run are visible to later
   skill tools through `observed_public_evidence` and
   `structured_job_candidates`.
2. The current delegation objective is used as `task_goal` for business
   handlers; private context remains bounded and never enters model-visible
   tool output.
3. Tool execution re-checks skill isolation at the adapter boundary.
4. Public request governor defaults are enabled for controller-created skill
   contexts.
5. External handoff and repeated-failure conditions terminate the active
   deepagents loop with the same trusted error codes as the pi harness.
6. Per-skill budget limits and model routing remain bounded and auditable;
   run-level accounting remains cumulative across attempts.
7. Chain seed artifacts preserve valid artifact types and provenance.
8. Single and chain evaluation records validate against `pi_eval_record_v1`,
   include audit output, and can be compared with existing baselines.
9. Deepagents unit tests cover the adapter, projection, middleware gates,
   controller smoke path, chain propagation, and record validation.
10. A fresh checkout can resolve `pi-py-career-skills` from this repository;
    the deepagents project is included in the workspace or uses an explicit
    local source, and generated caches are not packaged.

## Acceptance criteria

- A seeded public JD is present in the matching subagent's tool context.
- A discovery → matching chain can complete with a deterministic fake model
  and inherited evidence.
- A blocked public source stops the active delegation and preserves its error
  code in the run result and events.
- Deepagents evaluation output passes the shared schema validator and the
  shared audit functions.
- Deepagents tests pass together with the existing `pi-career-skills` tests.
- `uv tree --directory deepagents_packages` resolves successfully.
