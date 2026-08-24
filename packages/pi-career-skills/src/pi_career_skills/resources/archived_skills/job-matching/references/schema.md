# Job Matching Schema

| Field | Meaning |
|---|---|
| `artifact_id` | Persisted evidence artifact identifier; tool-side authority |
| `candidate_id` | Stable candidate identifier inside a structured JD artifact |
| `source_url` | Public source URL returned by the fetch tool |
| `content_hash` | Hash binding the report to the observed source |
| `score` | Deterministic bounded match score |
| `strengths` | Candidate facts supported by the target JD |
| `gaps` | Missing or conflicting confirmed facts |
| `unverified_criteria` | Criteria that cannot be judged from confirmed facts |

`candidate_id` may be resolved through the source artifact, but it never
replaces the persisted evidence binding.
