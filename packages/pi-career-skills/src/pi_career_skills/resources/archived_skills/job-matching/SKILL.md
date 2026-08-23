---
name: job-matching
description: >
  Rank observed job evidence against confirmed candidate facts and explicit preferences.
  Use for岗位匹配、职位筛选、岗位排序、匹配度分析 and related requests.
compatibility: requires the PEV career adapter's observed JD evidence and confirmed profile facts
---

# Job Matching Skill

Produce a reviewable, evidence-grounded job matching report. This package is the
authoritative source for matching policy; the PEV runtime only supplies lifecycle,
authorization, budgets, and the deterministic adapter tool.

## Required inputs

- `confirmed_profile_facts`: facts explicitly confirmed by the candidate. Never
  infer experience, education, skills, location, salary, or degree from silence.
- Observed job evidence produced by `job-discovery`, identified by persisted
  `artifact_id`, `source_url`, and `content_hash`.
- Explicit preferences such as role family, location, company, compensation,
  work mode, and time window. Missing preferences are not negative preferences.

If the target evidence is missing but an allowed `job-discovery` step can obtain
it from a public source, use that preceding step. Ask the user only when the
requested source is private, inaccessible, or explicitly user-supplied.

## Matching policy

1. Match only observed jobs. A model-proposed URL or invented JD is not evidence.
2. Keep `match_score`, strengths, gaps, and unverified criteria separate. A
   missing fact is `unverified`, not a failed requirement.
3. Prefer precision over fabricated completeness: return an honest empty result
   when no observed job satisfies the explicit criteria.
4. Preserve the candidate/job evidence pointers in every report row so later
   `resume-tailoring` and `career-planning` steps can resolve the same target.
5. Do not claim an application was submitted, and do not expose unverified or
   rejected job records as verified recommendations.

## Output contract

The adapter tool `match-observed-jobs` must produce a structured
`job_matching_report`. It must include the candidate pointer, source URL/hash,
score, strengths, gaps, and unverified criteria for each returned match. The
report is a recommendation for human review, not an application action.

## Boundaries

This Skill does not browse, authenticate, bypass anti-bot controls, mutate the
profile, or submit an application. If the required JD evidence is unavailable,
request `job-discovery` as a separate step or ask the user for a public source.

## References

- `references/schema.md` — stable adapter input/output and evidence-pointer fields.
