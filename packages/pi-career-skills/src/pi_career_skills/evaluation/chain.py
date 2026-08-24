"""Chain evaluation driver — multi-link runs with evidence inheritance.

Port of migration plan §10.2 and §11 (chain projection contract). Each link
is a fresh controller run; link N+1 only executes when link N succeeded,
inheriting candidate URLs, bounded evidence, and structured candidates.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..contracts import Artifact
from ..runtime.budgets import BudgetLimits
from ..runtime.controller import CareerRunController, RunRequest
from .audit import audit_chain
from .profile_facts import build_profile_facts
from .runner import _run_link_inner
from .schema import validate_record
from .seed_urls import ALL_SKILLS, resolve_seed_urls

#: Artifact types that carry a real, matchable job payload.
_JOB_BEARING_ARTIFACT_TYPES = frozenset(
    {"public_job_page", "structured_job_details"}
)

#: Total UTF-8 byte budget for inherited visible_text.
_MAX_INHERITED_EVIDENCE_BYTES = 24_000

#: Total UTF-8 byte budget for inherited structured_job_candidates.
_MAX_STRUCTURED_CANDIDATE_BYTES = 32_000

#: Maximum number of structured candidates promoted to the next link.
_MAX_STRUCTURED_CANDIDATES = 12

#: Maximum characters of previous-link summary carried in chain_context.
_MAX_CHAIN_SUMMARY_CHARS = 200


def _cap_utf8_bytes(value: str, budget: int) -> str:
    """Trim value so its UTF-8 encoding fits in budget bytes (CJK-aware)."""
    if len(value.encode("utf-8")) <= budget:
        return value
    low, high = 0, len(value)
    while low < high:
        mid = (low + high + 1) // 2
        if len(value[:mid].encode("utf-8")) <= budget:
            low = mid
        else:
            high = mid - 1
    return value[:low]


def _bounded_inherited_evidence(artifacts: list[dict]) -> list[dict]:
    """Project job-bearing link artifacts into observed_public_evidence shape.

    Only public_job_page / structured_job_details artifacts with a non-empty
    visible_text are promoted.  Visible text is capped by UTF-8 bytes so a
    CJK-heavy page cannot push the inherited projection past the ceiling.
    """
    bounded: list[dict] = []
    remaining_bytes = _MAX_INHERITED_EVIDENCE_BYTES
    for artifact in artifacts:
        artifact_type = artifact.get("artifact_type")
        if artifact_type not in _JOB_BEARING_ARTIFACT_TYPES:
            continue
        content = artifact.get("content_json") or {}
        visible = content.get("visible_text")
        if not isinstance(visible, str) or not visible.strip():
            continue
        visible_text = _cap_utf8_bytes(visible, remaining_bytes)
        item = {
            "source_url": artifact.get("source_url") or "",
            "content_hash": artifact.get("content_hash") or "",
            "artifact_id": artifact.get("artifact_id") or "",
            "artifact_type": artifact_type,
            "visible_text": visible_text,
        }
        bounded.append(item)
        remaining_bytes -= len(visible_text.encode("utf-8"))
        if remaining_bytes <= 0:
            break
    return bounded


def _structured_candidates_from_artifacts(
    artifacts: list[dict],
) -> list[dict]:
    """Derive structured_job_candidates from structured_job_details artifacts.

    Each structured_job_details artifact's content_json holds an extracted
    candidate list; we promote those referencing a real public source so
    the next link can rank them directly.  Fields are truncated and the
    whole list is byte-bounded.
    """
    candidates: list[dict] = []
    total_bytes = 0
    for artifact in artifacts:
        if len(candidates) >= _MAX_STRUCTURED_CANDIDATES:
            break
        if artifact.get("artifact_type") != "structured_job_details":
            continue
        content = artifact.get("content_json") or {}
        extracted = content.get("candidates") or content.get("details") or []
        for candidate in extracted:
            if len(candidates) >= _MAX_STRUCTURED_CANDIDATES:
                break
            if not isinstance(candidate, dict):
                continue
            source_url = candidate.get("source_url") or artifact.get(
                "source_url"
            )
            if not isinstance(source_url, str) or not source_url:
                continue
            projected = {
                "artifact_id": candidate.get("artifact_id")
                or artifact.get("artifact_id"),
                "source_url": source_url,
                "content_hash": candidate.get("content_hash")
                or artifact.get("content_hash"),
                "candidate_id": candidate.get("candidate_id")
                or candidate.get("id")
                or f"{artifact.get('artifact_id')}:candidate:{len(candidates)}",
                "title": candidate.get("title"),
                "company": candidate.get("company_name")
                or candidate.get("company"),
                "locations": candidate.get("locations")
                or (
                    [candidate["location"]]
                    if candidate.get("location")
                    else []
                ),
                "responsibilities": _cap_utf8_bytes(
                    candidate.get("responsibilities") or "", 4_000
                ),
                "requirements": _cap_utf8_bytes(
                    candidate.get("requirements") or "", 4_000
                ),
                "source_quality": candidate.get("source_quality")
                or "jd_complete",
                "page_text_prefix": _cap_utf8_bytes(
                    candidate.get("page_text_prefix") or "", 2_000
                ),
            }
            candidate_bytes = len(
                json.dumps(
                    projected, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
            if total_bytes + candidate_bytes > _MAX_STRUCTURED_CANDIDATE_BYTES:
                break
            total_bytes += candidate_bytes
            candidates.append(projected)
    return candidates


def _inherited_goal_supplement(
    structured_candidates: list[dict] | None,
    profile_facts: dict[str, str],
) -> str:
    """Render a compact, model-visible summary of inherited chain data.

    Uses reference-only inherited job context. Business fields remain in
    persisted seed artifacts and are never copied into the downstream goal.
    """
    lines: list[str] = []
    if structured_candidates:
        refs = [
            str(candidate.get("candidate_id") or candidate.get("artifact_id"))
            for candidate in structured_candidates[:8]
            if candidate.get("candidate_id") or candidate.get("artifact_id")
        ]
        lines.append(
            "【上一环节已收集的岗位】已持久化 "
            f"{len(structured_candidates)} 条候选，见 seed artifacts；"
            + ("证据 refs: " + ", ".join(refs) if refs else "")
        )
    if profile_facts:
        lines.append("【候选人已确认事实（简历）】")
        for key, value in list(profile_facts.items())[:12]:
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _candidate_urls_from_artifacts(artifacts: list[dict]) -> list[str]:
    """Extract source URLs from job-bearing artifacts."""
    return [
        a["source_url"]
        for a in artifacts
        if a.get("source_url")
        and a.get("artifact_type") in _JOB_BEARING_ARTIFACT_TYPES
    ]


def _chain_context_note(prev_id: str, prev_summary: str | None) -> str | None:
    """Build a chain_context note from the previous link's summary (≤200 chars)."""
    summary = (prev_summary or "").strip()
    if not summary:
        return None
    return (
        f"上一环节（{prev_id}）已完成岗位收集；本环节继承其工具产出的来源证据，"
        f"如需补充来源只能使用公开且安全的 URL。上一环节成果参考："
        f"{summary[:_MAX_CHAIN_SUMMARY_CHARS]}"
    )

def _build_seed_artifacts(
    inherited_evidence: list[dict],
    structured_candidates: list[dict],
) -> list[Artifact]:
    """Build seed Artifact objects from inherited projections.

    Returns a list of ``Artifact`` instances that can be passed to
    ``RunRequest.seed_artifacts`` for the next link.
    """
    seeds: list[Artifact] = []
    # observed_public_evidence → public_job_page seed artifacts
    for item in inherited_evidence:
        seeds.append(
            Artifact(
                artifact_id=item.get("artifact_id") or "",
                artifact_type=item.get("artifact_type") or "public_job_page",
                tool_name="fetch-public-job-pages",
                source_url=item.get("source_url") or None,
                content_hash=item.get("content_hash") or None,
                quality="jd_complete",
                content={"visible_text": item.get("visible_text", "")},
            )
        )
    # structured_job_candidates → structured_job_details seed artifacts
    if structured_candidates:
        by_artifact: dict[str, list[dict]] = {}
        for cand in structured_candidates:
            aid = cand.get("artifact_id") or f"seed-cand-{len(by_artifact)}"
            by_artifact.setdefault(aid, []).append(cand)
        for aid, cands in by_artifact.items():
            first = cands[0]
            seeds.append(
                Artifact(
                    artifact_id=aid,
                    artifact_type="structured_job_details",
                    tool_name="extract-observed-job-details-batch",
                    source_url=first.get("source_url"),
                    content_hash=first.get("content_hash"),
                    quality=first.get("source_quality") or "jd_complete",
                    content={"candidates": deepcopy(cands)},
                )
            )
    return seeds


async def run_chain(
    cid: str,
    *,
    question_dir: Path,
    out_dir: Path,
    model_id: str,
    controller_factory: Callable[..., CareerRunController] | None = None,
    work_root: Path | None = None,
) -> dict[str, Any]:
    """Run one chained question; link N+1 only runs when link N succeeded.

    Returns an EvalChainRecord dict.  Accepts controller_factory for test
    injection of stub-registry controllers.
    """
    from .runner import default_controller_factory

    started = time.monotonic()
    doc = json.loads(
        (question_dir / f"{cid}.json").read_text(encoding="utf-8")
    )
    links = doc.get("chain")
    if not isinstance(links, list) or not links:
        raise ValueError(
            f"{cid}: 'chain' must be a non-empty list of question docs"
        )

    factory = controller_factory or default_controller_factory(model_id)
    work_base = work_root or (out_dir / "_work")

    link_records: list[dict[str, Any]] = []
    confirmed_facts_by_link: list[dict[str, str]] = []

    for index, link_doc in enumerate(links, start=1):
        link_id = f"{cid}-L{index}"
        meta = link_doc.get("meta", {})
        profile = link_doc.get("profile", {})
        profile_facts = build_profile_facts(profile)
        confirmed_facts_by_link.append(profile_facts)

        # Resolve seeds (only link 1 has seeds by design).
        if index == 1:
            seeded_urls, _seed_note = resolve_seed_urls(link_id)
        else:
            seeded_urls = []

        # Broken chain — stop if previous link did not succeed.
        if index > 1 and link_records:
            prev = link_records[-1]
            if prev.get("result", {}).get("status") != "succeeded":
                break

        # Build the task goal.
        goal = link_doc.get("question", "")
        seed_artifacts: list[Artifact] | None = None
        inherited_evidence: list[dict] = []
        structured_candidates: list[dict] = []

        if index > 1 and link_records:
            prev = link_records[-1]
            prev_artifacts = prev.get("artifacts", [])

            inherited_evidence = _bounded_inherited_evidence(prev_artifacts)
            structured_candidates = _structured_candidates_from_artifacts(
                prev_artifacts
            )

            # Goal supplement — visible to the model: previous-link summary
            # note + structured candidates + confirmed facts.
            note = _chain_context_note(
                str(prev.get("id", "")),
                prev.get("result", {}).get("summary"),
            )
            supplement = _inherited_goal_supplement(
                structured_candidates, profile_facts
            )
            if note:
                supplement = note + (("\n\n" + supplement) if supplement else "")
            if supplement:
                goal = goal + "\n\n" + supplement

            # Seed artifacts for the evidence store.
            seed_artifacts = _build_seed_artifacts(
                inherited_evidence, structured_candidates
            )

        # Build task with seed URL supplement (link 1 only).
        task = goal
        if seeded_urls:
            task = (
                task
                + "\n\n请优先从以下种子链接收集证据：\n"
                + "\n".join(seeded_urls)
            )

        needed_skills = tuple(meta.get("skills") or ALL_SKILLS)

        attempt_id = uuid.uuid4().hex
        work_dir = work_base / link_id / attempt_id
        work_dir.mkdir(parents=True, exist_ok=True)

        request = RunRequest(
            task=task,
            user_id=f"eval-{link_id}",
            run_id=f"run-{link_id}",
            allowed_skills=tuple(ALL_SKILLS),
            needed_skills=needed_skills,
            budget=BudgetLimits(wall_clock_seconds=900),
            seed_artifacts=seed_artifacts,
            private_context={
                "confirmed_profile_facts": dict(profile_facts),
                "observed_public_evidence": deepcopy(inherited_evidence),
                "structured_job_candidates": deepcopy(structured_candidates),
            },
        )

        controller = factory()
        link_started = time.monotonic()

        full_record = await _run_link_inner(
            qid=link_id,
            question=link_doc.get("question", ""),
            meta=meta,
            request=request,
            controller=controller,
            started=link_started,
            seeded_urls=seeded_urls,
            profile=profile,
            attempt_id=attempt_id,
        )

        # Adapt to EvalChainLinkRecord shape (no runtime/model at link level).
        link_record: dict[str, Any] = {
            "id": link_id,
            "question": full_record["question"],
            "meta": full_record["meta"],
            "config": full_record["config"],
            "result": full_record["result"],
            "attempts": full_record["attempts"],
            "artifacts": full_record["artifacts"],
            "events": full_record["events"],
            "budget": full_record["budget"],
            "audit": full_record["audit"],
            "wall_seconds": full_record["wall_seconds"],
        }

        link_records.append(link_record)

    # Assemble chain-level record.
    last = link_records[-1]
    all_succeeded = all(
        r.get("result", {}).get("status") == "succeeded"
        for r in link_records
    )
    chain_complete = len(link_records) == len(links)
    aggregate_status = (
        "succeeded"
        if all_succeeded and chain_complete
        else last.get("result", {}).get("status", "failed")
    )

    record: dict[str, Any] = {
        "schema_version": "pi_eval_record_v1",
        "id": cid,
        "type": "chain",
        "chain_length": len(link_records),
        "links": link_records,
        "result": {
            "status": aggregate_status,
            "error_code": last.get("result", {}).get("error_code"),
            "summary": last.get("result", {}).get("summary"),
        },
        "audit": {},
        "wall_seconds": round(time.monotonic() - started, 1),
    }

    # Audit the chain.
    try:
        record["audit"] = audit_chain(
            record, confirmed_facts_by_link=confirmed_facts_by_link
        )
    except Exception:  # noqa: BLE001 — audit is best-effort
        record["audit"] = {"status": "inconclusive", "reason": "audit_exception"}

    # Validate.
    validate_record(record)

    # Write via temp + atomic rename.
    os.makedirs(str(out_dir), exist_ok=True)
    out_path = out_dir / f"{cid}.json"
    tmp_path = out_path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp_path, out_path)

    return record


__all__ = [
    "run_chain",
    "_cap_utf8_bytes",
    "_bounded_inherited_evidence",
    "_structured_candidates_from_artifacts",
    "_inherited_goal_supplement",
    "_MAX_INHERITED_EVIDENCE_BYTES",
    "_MAX_STRUCTURED_CANDIDATE_BYTES",
    "_MAX_STRUCTURED_CANDIDATES",
    "_MAX_CHAIN_SUMMARY_CHARS",
    "_JOB_BEARING_ARTIFACT_TYPES",
]
