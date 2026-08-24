"""Focused runtime evidence-to-context projection tests."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pi_career_skills.business.job_discovery.handlers import (
    extract_observed_job_details,
)
from pi_career_skills.business.job_discovery.models import ExtractObservedJobDetailsInput
from pi_career_skills.business.job_discovery.target_evidence import (
    resolve_target_evidence,
)
from pi_career_skills.context import ToolContext
from pi_career_skills.contracts import Artifact, ToolObservation
from pi_career_skills.runtime.context_projection import RuntimeContextProjection
from pi_career_skills.runtime.controller import CareerRunController
from pi_career_skills.runtime.evidence import EvidenceStore


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _page(
    *, artifact_id: str, source_url: str, content_hash: str, visible_text: str
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "artifact_type": "public_job_page",
        "source_url": source_url,
        "content_hash": content_hash,
        "quality": "jd_complete",
        "visible_text": visible_text,
    }


def _add_page(store: EvidenceStore, page: dict[str, Any]) -> None:
    store.add_observation(
        ToolObservation(
            tool_name="fetch-public-job-pages", status="succeeded", output={"pages": [page]}
        )
    )


def _candidate(
    *, artifact_id: str, source_url: str, content_hash: str, title: str
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "candidate_id": f"{artifact_id}:candidate:0",
        "source_artifact_id": artifact_id,
        "source_url": source_url,
        "content_hash": content_hash,
        "title": title,
        "company_name": "示例科技",
        "locations": ["北京"],
        "responsibilities": "负责Java服务端架构设计、核心功能开发和系统维护。",
        "requirements": "三年以上Java和Spring Boot开发经验，熟悉MySQL。",
        "source_quality": "jd_complete",
    }


def _add_candidates(store: EvidenceStore, candidates: list[dict[str, Any]]) -> None:
    store.add_observation(
        ToolObservation(
            tool_name="extract-observed-job-details-batch",
            status="succeeded",
            output={
                "details": [
                    {
                        "source_artifact_id": candidate["source_artifact_id"],
                        "source_url": candidate["source_url"],
                        "content_hash": candidate["content_hash"],
                        "source_quality": "jd_complete",
                        "candidates": [candidate],
                    }
                    for candidate in candidates
                ]
            },
        )
    )


def _add_candidate_page(store: EvidenceStore, candidates: list[dict[str, Any]]) -> None:
    first = candidates[0]
    store.add_observation(
        ToolObservation(
            tool_name="extract-observed-job-details-batch",
            status="succeeded",
            output={
                "details": [
                    {
                        "source_artifact_id": first["source_artifact_id"],
                        "source_url": first["source_url"],
                        "content_hash": first["content_hash"],
                        "source_quality": "jd_complete",
                        "candidates": candidates,
                    }
                ]
            },
        )
    )


def _seed_artifact(store: EvidenceStore, artifact: Artifact) -> None:
    seeder = object.__new__(CareerRunController)
    seeder._seed_artifact(store, artifact)


def _seed_page(
    *, artifact_id: str, source_url: str, content_hash: str, visible_text: str, **content: Any
) -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        artifact_type="public_job_page",
        tool_name="fetch-public-job-pages",
        source_url=source_url,
        content_hash=content_hash,
        quality="jd_complete",
        content={"visible_text": visible_text, **content},
    )


def _seed_candidate(artifact_id: str, candidate: dict[str, Any]) -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        artifact_type="structured_job_details",
        tool_name="extract-observed-job-details-batch",
        source_url=candidate["source_url"],
        content_hash=candidate["content_hash"],
        quality="jd_complete",
        content={"candidates": [candidate]},
    )


def test_refresh_prioritizes_new_store_pages_and_retains_source_aliases() -> None:
    """Current evidence must not be squeezed out by a full inherited budget."""
    first_hash = _hash("first")
    second_hash = _hash("second")
    new_hash = _hash("new")
    inherited = [
        _page(
            artifact_id="inherited-first",
            source_url="https://example.com/first",
            content_hash=first_hash,
            visible_text="a" * 12_000,
        ),
        _page(
            artifact_id="inherited-second",
            source_url="https://example.com/second",
            content_hash=second_hash,
            visible_text="b" * 12_000,
        ),
    ]
    confirmed_facts = [{"field": "skill", "value": "Java"}]
    projection = RuntimeContextProjection(
        {
            "confirmed_profile_facts": confirmed_facts,
            "observed_public_evidence": inherited,
        }
    )
    metadata = projection.initial_metadata(EvidenceStore())
    store = EvidenceStore()
    _add_page(
        store,
        _page(
            artifact_id="current-first-alias",
            source_url="https://example.com/first",
            content_hash=first_hash,
            visible_text="a" * 12_000,
        ),
    )
    _add_page(
        store,
        _page(
            artifact_id="current-new",
            source_url="https://example.com/new",
            content_hash=new_hash,
            visible_text="new Java JD",
        ),
    )

    projection.refresh(metadata, store)

    evidence = metadata["observed_public_evidence"]
    assert metadata["confirmed_profile_facts"] == confirmed_facts
    assert evidence[0]["artifact_id"] == "current-first-alias"
    assert any(item["artifact_id"] == "current-new" for item in evidence)
    first = next(item for item in evidence if item["source_url"].endswith("/first"))
    assert set(first["artifact_aliases"]) == {
        "current-first-alias",
        "inherited-first",
        f"observed:{first_hash}",
    }
    assert sum(
        item["source_url"].endswith("/first") for item in evidence
    ) == 1
    resolved = extract_observed_job_details(
        ToolContext(user_id="user", run_id="run", metadata=metadata),
        ExtractObservedJobDetailsInput(artifact_id=f"observed:{first_hash}"),
    )
    assert resolved.source_url == "https://example.com/first"


def test_refresh_uses_original_inherited_candidate_snapshot_after_truncation() -> None:
    """Refresh reuses the original inherited candidates, then puts store first."""
    inherited_candidates = [
        _candidate(
            artifact_id=f"inherited-{index}",
            source_url=f"https://example.com/{index}",
            content_hash=_hash(f"inherited-{index}"),
            title=f"Java 工程师 {index}",
        )
        for index in range(12)
    ]
    for candidate in inherited_candidates:
        candidate["responsibilities"] = "r" * 12_000
        candidate["requirements"] = "q" * 12_000
    inherited_candidates[0]["candidate_id"] = "shared-role"
    projection = RuntimeContextProjection(
        {"structured_job_candidates": inherited_candidates}
    )
    metadata = projection.initial_metadata(EvidenceStore())
    first_pass = metadata["structured_job_candidates"]
    assert len(first_pass) < len(inherited_candidates)
    first_pass[0]["title"] = "mutated context"
    projection.refresh(metadata, EvidenceStore())
    assert metadata["structured_job_candidates"][0]["title"] == "Java 工程师 0"

    store = EvidenceStore()
    aliased_candidate = _candidate(
        artifact_id="current-candidate-alias",
        source_url="https://example.com/0",
        content_hash=_hash("inherited-0"),
        title="Java 工程师 0",
    )
    aliased_candidate["candidate_id"] = "shared-role"
    new_candidate = _candidate(
        artifact_id="current-new-candidate",
        source_url="https://example.com/current",
        content_hash=_hash("current"),
        title="Java 后端开发工程师",
    )
    _add_candidates(store, [aliased_candidate, new_candidate])
    projection.refresh(metadata, store)

    candidates = metadata["structured_job_candidates"]
    assert candidates[0]["artifact_id"] == "current-candidate-alias"
    assert candidates[1]["artifact_id"] == "current-new-candidate"
    assert set(candidates[0]["artifact_aliases"]) == {
        "current-candidate-alias",
        "inherited-0",
        f"observed:{_hash('inherited-0')}",
    }


def test_seeded_pages_do_not_squeeze_live_page_from_context_projection() -> None:
    """Live pages rank before full chain seeds inserted earlier in the store."""
    store = EvidenceStore()
    for index in range(2):
        _seed_artifact(
            store,
            _seed_page(
                artifact_id=f"seed-page-{index}",
                source_url=f"https://example.com/seed-page-{index}",
                content_hash=_hash(f"seed-page-{index}"),
                visible_text="seed" * 3_000,
            ),
        )
    _add_page(
        store,
        _page(
            artifact_id="live-page",
            source_url="https://example.com/live-page",
            content_hash=_hash("live-page"),
            visible_text="live Java JD",
        ),
    )

    metadata = RuntimeContextProjection(
        {"confirmed_profile_facts": [{"field": "skill", "value": "Java"}]}
    ).initial_metadata(store)

    assert metadata["observed_public_evidence"][0]["artifact_id"] == "live-page"


def test_seeded_candidates_do_not_squeeze_live_candidate_from_context_projection() -> None:
    """Live structured candidates rank before a full chain-seeded candidate pool."""
    store = EvidenceStore()
    for index in range(12):
        candidate = _candidate(
            artifact_id=f"seed-candidate-{index}",
            source_url=f"https://example.com/seed-candidate-{index}",
            content_hash=_hash(f"seed-candidate-{index}"),
            title=f"Seed Java 工程师 {index}",
        )
        candidate["responsibilities"] = "r" * 1_200
        candidate["requirements"] = "q" * 1_200
        _seed_artifact(store, _seed_candidate(f"seed-structured-{index}", candidate))
    live_candidate = _candidate(
        artifact_id="live-candidate",
        source_url="https://example.com/live-candidate",
        content_hash=_hash("live-candidate"),
        title="Live Java 后端开发工程师",
    )
    _add_candidates(store, [live_candidate])

    metadata = RuntimeContextProjection(
        {"confirmed_profile_facts": [{"field": "skill", "value": "Java"}]}
    ).initial_metadata(store)

    assert metadata["structured_job_candidates"][0]["artifact_id"] == "live-candidate"


def test_candidate_projection_keeps_multiple_roles_on_one_page_and_merges_aliases() -> None:
    """Candidate identity is role-level, not only page-level."""
    source_url = "https://example.com/jobs"
    content_hash = _hash("jobs")
    inherited = _candidate(
        artifact_id="seed-java",
        source_url=source_url,
        content_hash=content_hash,
        title="Java 后端开发工程师",
    )
    inherited["candidate_id"] = "java-role"
    live_java = _candidate(
        artifact_id="live-java",
        source_url=source_url,
        content_hash=content_hash,
        title="Java 后端开发工程师",
    )
    live_java["candidate_id"] = "java-role"
    live_python = _candidate(
        artifact_id="live-python",
        source_url=source_url,
        content_hash=content_hash,
        title="Python 后端开发工程师",
    )
    live_python["candidate_id"] = "python-role"
    store = EvidenceStore()
    _add_candidate_page(store, [live_java, live_python])

    metadata = RuntimeContextProjection(
        {"structured_job_candidates": [inherited]}
    ).initial_metadata(store)
    candidates = metadata["structured_job_candidates"]

    assert [candidate["candidate_id"] for candidate in candidates] == [
        "java-role",
        "python-role",
    ]
    assert set(candidates[0]["artifact_aliases"]) >= {"live-java", "seed-java"}


def test_seed_aliases_resolve_through_real_handler() -> None:
    """Original seed aliases remain valid selectors at the handler boundary."""
    store = EvidenceStore()
    _seed_artifact(
        store,
        _seed_page(
            artifact_id="seed-page",
            source_url="https://example.com/seed-page",
            content_hash=_hash("seed-page"),
            visible_text="Java 后端开发工程师\n岗位职责：负责Java服务开发。\n任职要求：熟悉Spring Boot。",
            artifact_aliases=["raw-seed-alias"],
        ),
    )
    seeded_candidate = _candidate(
        artifact_id="seed-candidate",
        source_url="https://example.com/seed-page",
        content_hash=_hash("seed-page"),
        title="Java 后端开发工程师",
    )
    seeded_candidate.update(
        {
            "artifact_aliases": ["candidate-seed-alias"],
            "source_artifact_id": "seed-source",
            "source_artifact_aliases": ["source-seed-alias"],
        }
    )
    _seed_artifact(store, _seed_candidate("seed-structured", seeded_candidate))
    metadata = RuntimeContextProjection({}).initial_metadata(store)
    context = ToolContext(user_id="user", run_id="run", metadata=metadata)

    for selector in (
        "raw-seed-alias",
        "candidate-seed-alias",
        "source-seed-alias",
    ):
        result = extract_observed_job_details(
            context, ExtractObservedJobDetailsInput(artifact_id=selector)
        )
        assert result.source_url == "https://example.com/seed-page"


def test_projection_bounds_complete_serialized_records_and_skips_unsafe_inherited_data() -> None:
    """Projection limits apply to metadata JSON, not just JD text fields."""
    aliases = [f"alias-{index}-{'x' * 500}" for index in range(100)]
    oversized_page = _page(
        artifact_id="oversized-page",
        source_url="https://example.com/" + "u" * 5_000,
        content_hash=_hash("oversized-page"),
        visible_text="page",
    )
    oversized_page["artifact_aliases"] = aliases
    unsafe_page = _page(
        artifact_id="unsafe-page",
        source_url="https://example.com/unsafe",
        content_hash=_hash("unsafe-page"),
        visible_text="unsafe",
    )
    unsafe_page["unexpected"] = object()
    valid_page = _page(
        artifact_id="valid-page",
        source_url="https://example.com/valid",
        content_hash=_hash("valid-page"),
        visible_text="valid",
    )
    oversized_candidate = _candidate(
        artifact_id="oversized-candidate",
        source_url="https://example.com/" + "c" * 5_000,
        content_hash=_hash("oversized-candidate"),
        title="Java",
    )
    oversized_candidate["artifact_aliases"] = aliases
    valid_candidate = _candidate(
        artifact_id="valid-candidate",
        source_url="https://example.com/valid-candidate",
        content_hash=_hash("valid-candidate"),
        title="Java",
    )

    metadata = RuntimeContextProjection(
        {
            "observed_public_evidence": [oversized_page, unsafe_page, valid_page],
            "structured_job_candidates": [oversized_candidate, valid_candidate],
        }
    ).initial_metadata(EvidenceStore())

    evidence_json = json.dumps(
        metadata["observed_public_evidence"], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    candidates_json = json.dumps(
        metadata["structured_job_candidates"], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert len(evidence_json) <= 24_000
    assert len(candidates_json) <= 32_000
    assert [item["artifact_id"] for item in metadata["observed_public_evidence"]] == [
        "valid-page"
    ]
    assert [item["artifact_id"] for item in metadata["structured_job_candidates"]] == [
        "valid-candidate"
    ]


def test_seeded_no_id_candidates_merge_with_historical_projection_ids() -> None:
    """Re-seeded real details retain earlier generated candidate selectors."""
    upstream_store = EvidenceStore()
    details = []
    for index in range(12):
        source_url = f"https://example.com/history/{index}"
        content_hash = _hash(f"history-{index}")
        details.append(
            {
                "source_artifact_id": f"upstream-page-{index}",
                "source_url": source_url,
                "content_hash": content_hash,
                "source_quality": "jd_complete",
                "candidates": [
                    {
                        "title": f"Java 平台工程师 {index}",
                        "company_name": "示例科技",
                        "locations": ["北京"],
                        "responsibilities": "负责平台服务架构设计、核心能力建设和日常维护。",
                        "requirements": "熟悉Java、Spring Boot、MySQL和分布式系统。",
                    }
                ],
            }
        )
    upstream_store.add_observation(
        ToolObservation(
            tool_name="extract-observed-job-details-batch",
            status="succeeded",
            output={"details": details},
        )
    )
    upstream = RuntimeContextProjection({}).initial_metadata(upstream_store)[
        "structured_job_candidates"
    ]
    # Historical private context used the former child-store UUID fallback.
    # Keep that realistic predecessor selector while reusing real artifacts.
    for candidate, artifact in zip(upstream, upstream_store.artifacts(), strict=True):
        candidate["candidate_id"] = f"{artifact.artifact_id}:candidate:0"
    historical_ids = {candidate["candidate_id"] for candidate in upstream}
    assert len(upstream_store.artifacts()) == len(upstream) == 12

    chained_store = EvidenceStore()
    for artifact in upstream_store.artifacts():
        _seed_artifact(chained_store, artifact)
    metadata = RuntimeContextProjection(
        {"structured_job_candidates": upstream}
    ).initial_metadata(chained_store)
    projected = metadata["structured_job_candidates"]

    assert len(projected) == 12
    assert {candidate["source_url"] for candidate in projected} == {
        candidate["source_url"] for candidate in upstream
    }
    aliases = {
        alias for candidate in projected for alias in candidate["artifact_aliases"]
    }
    assert historical_ids <= aliases
    for historical_id in historical_ids:
        resolved = resolve_target_evidence([], projected, historical_id)
        assert resolved is not None


def test_projection_ignores_non_mapping_private_context() -> None:
    """Malformed top-level context never reaches the metadata projection."""
    for private_context in (["not", "a", "mapping"], "not-a-mapping", 7):
        metadata = RuntimeContextProjection(private_context).initial_metadata(
            EvidenceStore()
        )
        assert metadata == {
            "observed_public_evidence": [],
            "structured_job_candidates": [],
        }
