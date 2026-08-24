# Resume Tailoring - Schema

Field tables for the input, the diff object, and the validation output. Read this
when you need exact shapes.

## Input to `generate.py`

`--input PATH` or stdin must be a JSON object:

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `job_snapshot` | object | yes | The target job: `title`, `requirements` (list), optional `description` |
| `profile_facts` | object | yes | Candidate's confirmed facts; keys become the legal `valid_fact_refs` |
| `preferences` | object | no | e.g. `role_family`; passed through to the LLM |
| `match_analysis` | object | no | `strengths`/`gaps` lists; guides emphasis |

`generate.py` augments the payload with `valid_fact_refs = list(profile_facts.keys())`
before sending to the LLM.

## Diff object

Each entry in the `diffs` list:

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `op` | string | yes | One of `reorder`, `rephrase`, `summarize`, `omit`, `highlight` |
| `section` | string | yes | Non-empty resume section, e.g. `work_experience`, `projects`, `skills`, `summary` |
| `fact_ref` | string | yes | Must exist as a key in `profile_facts` (use a `valid_fact_refs` entry verbatim) |
| `before` | string | no | Current text; may be empty |
| `after` | string | no | Proposed tailored text; may be empty for `omit` |
| `evidence_ids` | list[string] | no | Each must exist in `evidence_refs` values; may be empty or omitted |

## `generate.py` output (`--out`)

On success:

```json
{ "status": "ok", "diffs": [ {...}, ... ], "agent_version": "1.0.0" }
```

On failure (exit 0, never a crash):

```json
{ "status": "failed", "code": "<code>", "last_error": "...", "agent_version": "1.0.0" }
```

Failure `code` values:

| code | meaning |
|------|---------|
| `missing_api_key` | No `DEEPSEEK_API_KEY`/`OPENAI_API_KEY` in env or Windows User scope |
| `draft_generation_interrupted` | LLM call raised (network/auth/timeout); `last_error` is the exception text |
| `draft_generation_parse_error` | LLM response had no parseable JSON / no `diffs` list |
| `bad_input` | Input file unreadable or not a JSON object |

`generate.py` stdout is always a one-line summary JSON with `status`, `code`,
`diff_count`, `agent_version`, and `out`.

## `validate.py` input

`--input PATH` (or stdin): a JSON list of diffs, or `{"diffs": [...]}`.

`--facts PATH`: a JSON object (the candidate's confirmed facts). Required.

`--evidence PATH`: a JSON object mapping a fact key to a list of evidence ids.
Optional; defaults to `{}`.

## `validate.py` output (`--out`)

On success:

```json
{ "status": "ok", "diff_count": 5 }
```

On failure (exit 0):

```json
{ "status": "failed", "code": "<code>", "index": 2, "last_error": "..." }
```

Validation `code` values (mirror `backend.app.services.draft_validators`):

| code | meaning |
|------|---------|
| `draft_validation_missing_op` | A diff has no `op` |
| `draft_validation_invalid_op` | `op` not in the allowlist |
| `draft_validation_empty_section` | `section` missing or empty |
| `draft_validation_invalid_fact_ref` | `fact_ref` not a key in confirmed facts |
| `draft_validation_invalid_evidence` | An `evidence_ids` entry not in `evidence_refs` values |
| `bad_input` | Input/facts/evidence unreadable or wrong type |
