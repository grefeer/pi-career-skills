"""Non-chain evaluation runner — single question runs through the controller.

Port of migration plan §10.1 record schema and §14 commands.  Each run
produces a single EvalRecord dict written atomically to <out>/<qid>.json.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..agents.prompts import PROMPT_HASHES
from ..model_factory import create_deepseek_model, resolve_api_key
from ..runtime.budgets import BudgetLimits
from ..runtime.controller import CareerRunController, RunRequest
from .audit import audit_record
from .profile_facts import build_profile_facts
from .schema import validate_record
from .seed_urls import ALL_SKILLS, resolve_seed_urls
from .url_utils import dedupe_seed_urls


def default_controller_factory(
    model_id: str,
) -> Callable[[], CareerRunController]:
    """Return a factory that builds a real controller for model_id.

    If model_id is "faux", uses the FAUX_MODEL test provider.  Otherwise
    builds a DeepSeek model descriptor.
    """
    if model_id == "faux":
        from pi_ai.providers.faux import FAUX_MODEL

        model = FAUX_MODEL
    else:
        model = create_deepseek_model(model_id)

    def _factory() -> CareerRunController:
        return CareerRunController(
            model,
            get_api_key=resolve_api_key,
        )

    return _factory


async def _run_link_inner(
    *,
    qid: str,
    question: str,
    meta: dict[str, Any],
    request: RunRequest,
    controller: CareerRunController,
    started: float,
    seeded_urls: list[str],
    profile: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any]:
    """Run one link / single question and assemble an EvalRecord-like dict.

    Shared between ``run_question`` and ``chain.run_chain`` so record
    assembly stays consistent.
    """
    result = await controller.run(request)
    wall_seconds = round(time.monotonic() - started, 3)

    # Artifacts with full content (for chain projection and audit).
    artifacts: list[dict[str, Any]] = list(result.artifacts)

    # Assemble event list (bounded payload — keep under 4096 bytes).
    events: list[dict[str, Any]] = []
    for evt in result.events:
        payload = evt.payload or {}
        # Quick bound — skip oversize payloads gracefully.
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 4096:
            payload = {"_payload_truncated": True}
        events.append({"type": evt.type, "payload": payload})

    # Build attempt list.
    attempts: list[dict[str, Any]] = [
        {
            "attempt_id": attempt_id,
            "status": result.status,
            "error_code": result.error_code,
            "summary": result.summary,
            "tool_calls": getattr(result.budget, "tool_calls", None),
            "events": events,
        }
    ]

    # Budget dict.
    budget_limits = request.budget or BudgetLimits()
    budget: dict[str, Any] = {
        "limits": {
            "agent_turns": budget_limits.agent_turns,
            "tool_calls": budget_limits.initial_tool_calls,
            "model_requests": budget_limits.model_requests,
            "input_tokens": budget_limits.input_tokens,
            "wall_clock_seconds": budget_limits.wall_clock_seconds,
            "auto_recoveries": budget_limits.auto_recoveries,
        },
        "consumed": {
            "agent_turns": result.budget.agent_turns,
            "tool_calls": result.budget.tool_calls,
            "model_requests": result.budget.model_requests,
            "input_tokens": result.budget.input_tokens,
            "wall_clock_seconds": result.budget.wall_clock_seconds,
            "auto_recoveries": result.budget.auto_recoveries,
        },
    }

    # Model info.
    model = controller._model  # noqa: SLF001 — internal read for metadata
    model_data: dict[str, str] = {
        "id": getattr(model, "id", ""),
        "provider": getattr(model, "provider", ""),
    }

    record: dict[str, Any] = {
        "schema_version": "pi_eval_record_v1",
        "id": qid,
        "type": "single",
        "question": question,
        "meta": meta,
        "runtime": {
            "name": "pi-career-skills",
            "version": "0.1.0",
        },
        "model": model_data,
        "config": {
            "prompt_hashes": dict(PROMPT_HASHES),
            "feature_flags": {},
            "seeded_urls": list(seeded_urls),
        },
        "result": {
            "status": result.status,
            "error_code": result.error_code,
            "summary": result.summary,
        },
        "attempts": attempts,
        "artifacts": artifacts,
        "events": events,
        "budget": budget,
        "audit": {},
        "wall_seconds": wall_seconds,
    }

    # Audit (best-effort; non-succeeded records get not_applicable).
    profile_facts = build_profile_facts(profile)
    try:
        record["audit"] = audit_record(
            record,
            inherited_refs=None,
            confirmed_facts=profile_facts,
        )
    except Exception:  # noqa: BLE001 — audit is best-effort in record
        record["audit"] = {
            "status": "inconclusive",
            "reason": "audit_exception",
            "checks": {},
        }

    return record


async def run_question(
    qid: str,
    *,
    question_dir: Path,
    out_dir: Path,
    model_id: str,
    controller_factory: Callable[..., CareerRunController] | None = None,
    work_root: Path | None = None,
    wall_clock_seconds: int = 600,
) -> dict[str, Any]:
    """Run one non-chain question and write the result record.

    Args:
        qid: Question id (e.g. "Q011").
        question_dir: Directory containing ``<qid>.json``.
        out_dir: Output directory for the record.
        model_id: Model identifier ("deepseek-v4-flash" or "faux").
        controller_factory: Optional factory callable for test injection.
        work_root: Optional root for per-attempt work directories.

    Returns:
        EvalRecord dict.
    """
    factory = controller_factory or default_controller_factory(model_id)
    doc = json.loads(
        (question_dir / f"{qid}.json").read_text(encoding="utf-8")
    )

    question = doc.get("question", "")
    meta = doc.get("meta", {})
    profile = doc.get("profile", {})

    normalized_urls = meta.get("target_urls") if isinstance(meta, dict) else None
    if isinstance(normalized_urls, list) and all(
        isinstance(url, str) and url.strip() for url in normalized_urls
    ):
        seeded_urls = dedupe_seed_urls(normalized_urls)
        original_urls, _seed_note = resolve_seed_urls(qid)
        for url in original_urls:
            # Normalized target URLs express the revised role family; the
            # curated parity seeds still identify the real source route (for
            # example Liepin landing pages) and must remain available.
            seeded_urls = dedupe_seed_urls([*seeded_urls, url])
    else:
        seeded_urls = dedupe_seed_urls(resolve_seed_urls(qid)[0])

    # Build task with seed URL supplement (record keeps original question).
    task = question
    if seeded_urls:
        task = (
            task
            + "\n\n请优先从以下种子链接收集证据：\n"
            + "\n".join(seeded_urls)
        )

    facts = build_profile_facts(profile)
    needed_skills = tuple(meta.get("skills") or ALL_SKILLS)
    allowed_skills = _allowed_skills_for_needed(needed_skills)

    request = RunRequest(
        task=task,
        user_id=f"eval-{qid}",
        run_id=f"run-{qid}",
        allowed_skills=allowed_skills,
        needed_skills=needed_skills,
        budget=BudgetLimits(wall_clock_seconds=wall_clock_seconds),
        private_context={
            "confirmed_profile_facts": facts,
            # Keep seed URLs machine-readable as well as in the task text;
            # discovery tools can then honor the direct-fetch rule without
            # relying on the model to copy URLs out of prose.
            "candidate_urls": list(seeded_urls),
            # Keep search deliberately bounded for evaluation and production:
            # an aggregator is a fallback source, not permission to fan out
            # across several gated providers.  The tool still deduplicates
            # queries and stops immediately after usable evidence appears.
            "search_route_budget": 2,
        },
    )

    work_base = work_root or (out_dir / "_work")
    attempt_id = uuid.uuid4().hex
    work_dir = work_base / qid / attempt_id
    work_dir.mkdir(parents=True, exist_ok=True)

    controller = factory()
    started = time.monotonic()
    record = await _run_link_inner(
        qid=qid,
        question=question,
        meta=meta,
        request=request,
        controller=controller,
        started=started,
        seeded_urls=seeded_urls,
        profile=profile,
        attempt_id=attempt_id,
    )

    # Validate the assembled record.
    validate_record(record)

    # Write atomically (temp + os.replace).
    os.makedirs(str(out_dir), exist_ok=True)
    out_path = out_dir / f"{qid}.json"
    tmp_path = out_path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp_path, out_path)

    return record


def _allowed_skills_for_needed(needed_skills: tuple[str, ...]) -> tuple[str, ...]:
    """Expose only requested capabilities plus the discovery prerequisite."""
    needed = set(needed_skills)
    if not needed:
        return tuple(ALL_SKILLS)
    allowed = {skill for skill in needed if skill in ALL_SKILLS}
    if allowed - {"job-discovery"}:
        allowed.add("job-discovery")
    return tuple(skill for skill in ALL_SKILLS if skill in allowed)


def main() -> None:
    """Module entry point — delegates to CLI."""
    from .cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()


__all__ = [
    "run_question",
    "ALL_SKILLS",
    "default_controller_factory",
    "_run_link_inner",
    "main",
]
