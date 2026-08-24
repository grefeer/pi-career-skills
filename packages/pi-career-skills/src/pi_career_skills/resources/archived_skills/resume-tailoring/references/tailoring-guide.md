# Resume Tailoring - Guide

When to use, the workflow, the prompt, and the tolerant JSON parse for the
resume-tailoring skill. Read this for the L2 single-job path.

## When to use

Use this skill when you (the LLM orchestrator) need to tailor a candidate's
resume to a specific job: the human has a target job and a confirmed profile, and
wants grounded, reviewable edits - not a free-text rewrite.

Do NOT use this skill to:

- Write a resume from scratch (there is no profile to diff against)
- Apply edits to the candidate's profile store (Phase 4 is human-controlled; this
  skill is read-only)
- Generate interview questions (that is the `interview-prep` skill)
- Track application status (that is the `application-tracking` skill)

## Workflow: generate -> validate -> apply

### 1. Assemble input

`generate.py` reads a JSON object with four fields (all but `job_snapshot` and
`profile_facts` are optional; see `schema.md`):

```json
{
  "job_snapshot": { "title": "...", "requirements": ["..."], "description": "..." },
  "profile_facts": { "work_experience": "...", "projects": "...", "skills": "..." },
  "preferences": { "role_family": "AI应用开发" },
  "match_analysis": { "strengths": ["..."], "gaps": ["..."] }
}
```

`profile_facts` keys are the candidate's confirmed facts. The script derives
`valid_fact_refs` from these keys and tells the LLM to use them verbatim - every
generated diff's `fact_ref` must be one of these.

### 2. Generate

```bash
python scripts/generate.py --input output/input.json --out output/draft_diffs.json
```

The script writes the full result to `--out` and prints a one-line summary to
stdout:

```
{"status": "ok", "code": null, "diff_count": 5, "agent_version": "1.0.0", "out": "output/draft_diffs.json"}
```

### 3. Validate

Before any human applies the diffs, ground-check them:

```bash
python scripts/validate.py --input output/draft_diffs.json \
  --facts output/profile_facts.json --evidence output/evidence_refs.json
```

Validation mirrors `backend.app.services.draft_validators.validate_draft_diffs`
exactly - same op allowlist, same error codes, same first-failure semantics. The
`--evidence` arg is optional; omit it when the profile has no evidence refs.

### 4. Apply (human-controlled)

The human reviews `output/draft_diffs.json` and applies the edits. This skill
does not write to the profile store.

## The prompt

The System prompt (embedded in `generate.py`, mirrored from the backend
`LLMDraftGenerator`) instructs the LLM to:

1. Emit a JSON object `{"diffs": [...]}` only - no prose, no markdown fences.
2. Give each diff an `op` in `{reorder, rephrase, summarize, omit, highlight}`.
3. Give each diff a non-empty `section` (e.g. `work_experience`, `projects`,
   `skills`, `summary`).
4. Give each diff a `fact_ref` that EXISTS in `profile_facts` - it must use one
   of the provided `valid_fact_refs` verbatim.
5. Optionally give each diff an `evidence_ids` list (may be empty or omitted).

The Human message is the serialized input payload, augmented with
`valid_fact_refs` so the LLM knows exactly which fact keys are legal.

## The tolerant JSON parse

LLMs do not always return clean JSON. `generate.py` imports the shared
`skill/_common/llm_json.py` helpers and tries, in order:

1. A fenced ```json ... ``` block (regex search).
2. The whole content.
3. A bracket slice (`{...}` or `[...]` between the first open and last close).

The first value that parses is used. Non-dict entries in the `diffs` list are
dropped (`coerce_diffs`). If nothing parses, or there is no `diffs` list, the
script returns `status=failed`, `code=draft_generation_parse_error` (exit 0).

## Credential resolution

`generate.py` resolves the API key in this order (mirrors `src/utils.py`):

1. `DEEPSEEK_API_KEY` in the process env
2. `OPENAI_API_KEY` in the process env
3. `DEEPSEEK_API_KEY` in Windows CURRENT_USER\Environment (User scope)
4. `OPENAI_API_KEY` in Windows User scope

The base URL defaults to `https://api.deepseek.com` (override with
`OPENAI_BASE_URL`). The model defaults to `deepseek-v4-flash` (override with
`--model` or `OPENAI_MODEL`). For `deepseek-v4` models on a deepseek base URL,
the script disables the interleaved `thinking` mode so JSON parses cleanly
(mirrors `backend.app.services.resume_tailoring.llm_factory`).

A missing key returns `status=failed`, `code=missing_api_key` (exit 0) - it does
not crash.
