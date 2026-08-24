---
name: career-planning
description: >
  Build an evidence-grounded job search, preparation, or interview plan from an observed target JD.
  Use for求职规划、面试准备计划、技能补齐计划、求职行动计划 and related requests.
compatibility: requires a persisted target JD artifact and confirmed profile facts when personalization is requested
---

# Career Planning Skill

Produce a structured, reviewable preparation plan for an observed target job.
This package owns career-planning policy; the PEV runtime owns only execution
control and verification routing.

## Required inputs

- One target JD resolved through an evidence pointer (`artifact_id`, `candidate_id`,
  `source_url`, and `content_hash`).
- Confirmed candidate facts when the plan is personalized.
- An explicit target date, if the user supplied one. Never invent a deadline.

If the user names a role but supplies no JD and an allowed `job-discovery` step
can obtain a public target, use that preceding step instead of requesting a
duplicate JD. Confirmed profile facts supplied by the server are available via
the runtime's scoped private context and must not be requested again.

## Planning policy

1. Resolve the target from persisted observed evidence. Never treat a model-written
   URL, title, or copied JD text as authoritative by itself.
2. Separate JD requirements, candidate strengths, skill gaps, and assumptions.
3. Every action item must state an observable outcome, an evidence basis, and a
   due-date rationale. If no target date exists, use an explicit relative order,
   not a fabricated calendar date.
4. Keep the plan proportional to the evidence. A missing target JD is a blocking
   input and must be reported honestly.
5. The plan prepares the candidate; it never submits applications or performs
   irreversible external actions.

## Output contract

The adapter tool `build-preparation-plan` must produce a structured
`career_preparation_plan` with target evidence references, prioritized items,
skill gaps, and any assumptions or unresolved inputs. A human reviews and acts
on the plan.

## Boundaries

This Skill does not browse, authenticate, bypass anti-bot controls, modify a
resume/profile, or submit an application. Use `job-discovery` or
`job-matching` as separate preceding steps when their outputs are needed.

## References

- `references/schema.md` — plan and target-evidence fields.
