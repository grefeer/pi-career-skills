# Career Planning Schema

| Field | Meaning |
|---|---|
| `target_artifact_id` | Persisted JD artifact used by the plan |
| `target_candidate_id` | Candidate inside a structured JD artifact |
| `items` | Ordered actions with outcome, rationale, and due-date mode |
| `skill_gaps` | Requirements not supported by confirmed candidate facts |
| `assumptions` | Explicitly unresolved facts; never hidden in prose |

Target resolution accepts `artifact_id`, `candidate_id`, `source_artifact_id`,
or the same persisted `source_url`, but the final output must retain the
canonical artifact pointer.
