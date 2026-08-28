"""Controlled artifact transport between deepagents evaluation chain links."""

from __future__ import annotations

from pi_career_skills.contracts import Artifact

from .contracts import RunResult


def build_seed_artifacts(prev_result: RunResult) -> list[Artifact]:
    """Rebuild prior durable artifacts as safe next-link seed artifacts.

    A downstream link receives job-bearing evidence through the same public
    page / structured-candidate projection used by the pi chain runner. Pure
    reports are therefore represented as public-page seeds, while structured
    candidate pools retain their candidate projection.
    """
    seeds: list[Artifact] = []
    for art in prev_result.artifacts:
        content = art.get("content_json") or {}
        if art.get("artifact_type") == "structured_job_details":
            candidates = content.get("candidates")
            if isinstance(candidates, list) and candidates:
                seeds.append(
                    Artifact(
                        artifact_id=art.get("artifact_id") or "seed-cand",
                        artifact_type="structured_job_details",
                        tool_name="extract-observed-job-details-batch",
                        source_url=art.get("source_url"),
                        content_hash=art.get("content_hash"),
                        quality=art.get("quality") or "jd_complete",
                        content={"candidates": candidates},
                    )
                )
            continue
        visible = content.get("visible_text") or content.get("summary") or ""
        seeds.append(
            Artifact(
                artifact_id=art.get("artifact_id") or "seed-page",
                artifact_type="public_job_page",
                tool_name="fetch-public-job-pages",
                source_url=art.get("source_url"),
                content_hash=art.get("content_hash"),
                quality=art.get("quality") or "jd_complete",
                content={"visible_text": visible if isinstance(visible, str) else ""},
            )
        )
    return seeds


__all__ = ["build_seed_artifacts"]
