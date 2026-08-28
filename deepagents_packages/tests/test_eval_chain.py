from __future__ import annotations

from deepagents_skills.chain import build_seed_artifacts
from deepagents_skills.contracts import RunResult

from pi_career_skills.runtime.budgets import BudgetConsumed


def test_non_structured_chain_artifacts_become_public_page_seeds() -> None:
    result = RunResult(
        run_id="run-1",
        status="succeeded",
        summary="上一环节完成",
        error_code=None,
        error_message=None,
        attempt_count=1,
        completed_skills=["job-matching"],
        refs=[],
        artifacts=[
            {
                "artifact_id": "matching-report",
                "artifact_type": "job_matching_report",
                "source_url": "https://example.com/job",
                "content_hash": "hash",
                "quality": None,
                "content_json": {"summary": "匹配结果"},
            }
        ],
        events=[],
        budget=BudgetConsumed(),
    )

    seeds = build_seed_artifacts(result)

    assert len(seeds) == 1
    assert seeds[0].artifact_type == "public_job_page"
