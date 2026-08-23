---
name: resume-tailoring
description: >
  Tailor a candidate's resume to a specific target job via LLM diff operations.
  Given a job snapshot and the candidate's confirmed profile facts, preferences,
  and a match analysis (strengths/gaps), generate a concise list of resume diff
  operations (reorder/rephrase/summarize/omit/highlight), each grounded in a
  confirmed fact and evidence. Use when the user wants to 改简历, 针对岗位改写简历,
  tailor a resume for a job, or produce resume suggestions. Also use when the user
  mentions "简历针对性改写", "针对岗位修改简历", "简历优化", or similar Chinese phrases.
compatibility: requires Python 3.10+, langchain-openai (pip), a configured DEEPSEEK_API_KEY (or OPENAI_API_KEY)
---

# Resume Tailoring Agent

Produce resume diff operations that tailor a candidate's resume to a target job.
Designed as a pi-agent skill - the LLM (you) orchestrates, helper scripts handle
the mechanical work (LLM call, JSON parsing, validation against confirmed facts).

**This file is a dispatch hub.** It is intentionally short. Load the reference file
that matches your task from the [Progressive disclosure](#progressive-disclosure-how-deep-to-go)
table or the [References](#references) list - do NOT read them all up front.

## Why this skill exists

Tailoring a resume to a job is a judgment task: which facts to surface, which to
trim, what to reorder for emphasis. A hand-written prompt per job does not scale
and drifts. Instead, this skill uses:

1. **A bounded LLM call** that emits structured diff operations (one prompt, parsed
   tolerantly into JSON)
2. **Fact grounding** - every diff references a `fact_ref` that MUST exist in the
   candidate's confirmed profile facts (validated before application)
3. **Evidence linking** - every `evidence_ids` entry MUST exist in the profile's
   evidence refs, so nothing is invented

The result is a list of reviewable, grounded operations a human (or the agent on
their behalf) applies to the resume - not a free-text rewrite.

## Security boundary (HARD)

- This skill is **read-only generation**. It never writes to the candidate's
  profile store and never auto-applies diffs. The human always applies the final
  resume.
- It never auto-submits anything (no `task:submit` scope exists anywhere in this
  skill).
- A missing API key or unparseable LLM response surfaces as `status=failed` with a
  stable `code` (exit 0) - it never escalates past the human.

## Quick start

```bash
# 1. Ensure dependencies + credentials
pip install langchain-openai
export DEEPSEEK_API_KEY=...   # or OPENAI_API_KEY (Windows User scope also works)

# 2. Prepare input (job snapshot + confirmed facts + preferences + match analysis):
#    see references/tailoring-guide.md and references/schema.md
cat > output/input.json <<'JSON'
{
  "job_snapshot": {"title": "...", "requirements": [...]},
  "profile_facts": {"work_experience": "...", "projects": "..."},
  "preferences": {"role_family": "AI应用开发"},
  "match_analysis": {"strengths": [...], "gaps": [...]}
}
JSON

# 3. Generate the diffs
python scripts/generate.py --input output/input.json --out output/draft_diffs.json

# 4. Validate the diffs against the confirmed facts + evidence refs BEFORE applying
python scripts/validate.py --input output/draft_diffs.json \
  --facts output/profile_facts.json --evidence output/evidence_refs.json

# 5. Review output/draft_diffs.json with the human; apply manually.
```

## Full workflow

When the user names a role but does not provide a target JD and the task is
authorized for `job-discovery`, plan or request a preceding public-evidence
capture step. Do not ask the user to paste a JD that the permitted discovery
Skill can obtain. When confirmed candidate facts are available from the server,
use them through the runtime's scoped private context; do not ask the user to
upload the same resume again.

There are four phases. The single-job path (L2) covers Phases 2-4; the
differential path (L3) re-runs against an updated match analysis.

### Phase 1 - INPUT: Assemble the generation context (L3 only)

Collect the job snapshot, the candidate's confirmed profile facts (with their
evidence refs), stated preferences, and a match analysis (strengths/gaps). Field
shapes live in **`references/schema.md`**.

### Phase 2 - GENERATE: Produce the diff operations

Run `scripts/generate.py`. It builds the System+Human prompt, calls the LLM, and
parses the response into a list of diff dicts. The prompt, the tolerant JSON
parse, and the credential resolution are documented in
**`references/tailoring-guide.md`**.

```bash
python scripts/generate.py --input output/input.json --out output/draft_diffs.json
```

### Phase 3 - VALIDATE: Ground every diff before applying

Run `scripts/validate.py` against the candidate's confirmed facts + evidence
refs. This mirrors the backend's `validate_draft_diffs` exactly (same op
allowlist, same error codes) so a hand-written diff is checked identically.

```bash
python scripts/validate.py --input output/draft_diffs.json \
  --facts output/profile_facts.json --evidence output/evidence_refs.json
```

A failed validation reports `status=failed`, a stable `code`
(`draft_validation_invalid_op` / `_empty_section` / `_invalid_fact_ref` /
`_invalid_evidence` / `_missing_op`), and the offending `index`. Fix that diff
and re-run.

### Phase 4 - APPLY: Human-controlled, never auto

Review `output/draft_diffs.json` with the human. The human applies the resume
edits. This skill does not write to the profile store.

## Error handling guide

| Situation | Action |
|-----------|--------|
| `status=failed`, `code=missing_api_key` | Set `DEEPSEEK_API_KEY`/`OPENAI_API_KEY` in env or Windows User scope; rerun |
| `status=failed`, `code=draft_generation_parse_error` | Re-run once; if it persists, ask the human to narrow the job snapshot |
| `status=failed`, `code=draft_generation_interrupted` | Transient LLM/network error; retry with backoff |
| `status=failed`, `code=bad_input` | Input JSON was malformed; fix the input file |
| `code=draft_validation_invalid_fact_ref` | The LLM referenced a fact not in `profile_facts`; re-generate or add the fact |
| `code=draft_validation_invalid_evidence` | A diff cites an evidence_id absent from `evidence_refs`; drop the id or add the evidence |
| `code=draft_validation_invalid_op` / `_empty_section` / `_missing_op` | Hand-edit the diff to use a valid op + non-empty section |

## References

Load these as needed during processing:

- `references/tailoring-guide.md` - When to use, the generate->validate->apply workflow, the prompt, and the tolerant JSON parse
- `references/schema.md` - Field tables for input, diff, and validation output

## Progressive disclosure: how deep to go

This skill is designed with usage levels. Start shallow; go deeper only when needed.

| Level | What you load | When to use |
|-------|---------------|-------------|
| **L1: Validate only** | `SKILL.md` + `scripts/validate.py` | You already have diffs (hand-written or from elsewhere); just ground-check them |
| **L2: Single job** | `SKILL.md` + `references/tailoring-guide.md` (+ `schema.md` as needed) | Tailor a resume for one job end-to-end |
| **L3: Differential re-tailor** | L2 + updated `match_analysis` in input | Re-run generation against a refreshed match analysis without redoing input assembly |

## Scripts

- `scripts/generate.py` - **L2**: LLM call -> tolerant JSON parse -> list of diff operations
- `scripts/validate.py` - **L1/L2**: Validate diffs against confirmed facts + evidence refs (mirrors backend `validate_draft_diffs`)

## PEV adapter boundary

When activated by the backend PEV runtime, this Skill owns the tailoring
decision, grounding rules, and user handoff policy. The runtime supplies only
the lifecycle, scoped confirmed facts, typed preceding artifacts, budgets, and
the deterministic `build-resume-tailoring-brief` adapter. If a public target JD
is missing and `job-discovery` is allowed, plan that preceding Skill instead of
asking the user to paste a JD. Missing optional preferences do not block a
grounded draft; ask one question only when the target evidence or confirmed
fact required by the output cannot be obtained through an allowed Skill.
