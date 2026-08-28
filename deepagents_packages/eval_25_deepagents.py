"""deepagents 版 25 题回归测试脚本（对齐 eval_results/questions/normalized 数据集）。

覆盖题目：
    - Q 题（11 道单环节）：Q011 Q017 Q028 Q034 Q040 Q045 Q046 Q055 Q081 Q144 Q148
    - R 题（4 道单环节）：R001 R024 R025 R043
    - C 题（10 道链式，每链 2-3 环节）：C001 C002 C003 C006 C007 C008 C010 C013 C014 C015

行为（与 pi evaluation/runner.py + chain.py 对齐）：
    - 每题组装 RunRequest：task = 原题 + 种子 URL 补充；private_context 携带
      confirmed_profile_facts / candidate_urls / search_route_budget；
    - allowed_skills = needed + discovery 前置（_allowed_skills_for_needed）；
    - C 链逐环节运行，上一环节的 artifacts 通过 ``seed_artifacts`` 投影到下一环节
      的 EvidenceStore，并在下一环节 task 前追加链上下文说明；
    - 结果写入 out_dir/<qid>.json，并打印汇总表。

用法：
    # 冒烟（无 API key，ScriptedFakeChatModel）
    .venv/Scripts/python.exe deepagents_packages/eval_25_deepagents.py --model faux
    # 真实运行（需要 DEEPSEEK_API_KEY）
    .venv/Scripts/python.exe deepagents_packages/eval_25_deepagents.py
    # 只跑某几题 / 限制数量 / 换输出目录
    .venv/Scripts/python.exe deepagents_packages/eval_25_deepagents.py --qids Q046 Q017 --out temp/eval_da
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from deepagents_skills.chain import build_seed_artifacts
from deepagents_skills.contracts import RunRequest, RunResult
from deepagents_skills.controller import CareerRunController
from deepagents_skills.models import (
    ScriptedFakeChatModel,
    create_deepseek_chat_model,
)

from pi_career_skills.agents.prompts import PROMPT_HASHES
from pi_career_skills.contracts import Artifact
from pi_career_skills.evaluation.audit import audit_chain, audit_record
from pi_career_skills.evaluation.profile_facts import build_profile_facts
from pi_career_skills.evaluation.schema import validate_record
from pi_career_skills.evaluation.seed_urls import ALL_SKILLS, resolve_seed_urls
from pi_career_skills.evaluation.url_utils import dedupe_seed_urls
from pi_career_skills.runtime.budgets import BudgetConsumed, BudgetLimits

#: 默认 25 题数据集目录（仓库根下的 normalized 归一化结果）。
DEFAULT_QUESTION_DIR = Path("eval_results") / "questions" / "normalized"
DEFAULT_OUT_DIR = Path("temp") / "eval_25_deepagents"

#: 链式环节 task 追加的上下文说明（对齐 pi chain.py:_chain_context_note）。
_MAX_CHAIN_SUMMARY_CHARS = 400


def _allowed_skills_for_needed(needed_skills: tuple[str, ...]) -> tuple[str, ...]:
    """Expose only requested capabilities plus the discovery prerequisite."""
    needed = set(needed_skills)
    if not needed:
        return tuple(ALL_SKILLS)
    allowed = {skill for skill in needed if skill in ALL_SKILLS}
    if "job-discovery" in ALL_SKILLS and (
        "job-matching" in allowed
        or "resume-tailoring" in allowed
        or "career-planning" in allowed
    ):
        allowed.add("job-discovery")
    return tuple(allowed)


def _load_question(question_dir: Path, qid: str) -> dict[str, Any]:
    doc = json.loads((question_dir / f"{qid}.json").read_text(encoding="utf-8"))
    if not doc.get("id"):
        doc["id"] = qid
    return doc


def _seeded_urls_for(qid: str, meta: dict[str, Any]) -> list[str]:
    """target_urls + curated parity seeds（对齐 runner.py:203-215）。"""
    normalized = meta.get("target_urls")
    seeded: list[str] = []
    if isinstance(normalized, list) and all(
        isinstance(url, str) and url.strip() for url in normalized
    ):
        seeded = dedupe_seed_urls(normalized)
    original_urls, _note = resolve_seed_urls(qid)
    return dedupe_seed_urls([*seeded, *original_urls])


def _build_request(
    qid: str,
    link_id: str,
    question: str,
    meta: dict[str, Any],
    profile: dict[str, Any],
    *,
    seed_urls: list[str],
    seed_artifacts: list[Artifact] | None = None,
    wall_clock_seconds: int = 600,
    chain_note: str | None = None,
    goal_supplement: str | None = None,
) -> RunRequest:
    """组装一个环节的 RunRequest（对齐 pi runner/chain 的字段语义）。"""
    task = question
    if goal_supplement:
        task = f"{task}\n\n{goal_supplement}"
    if chain_note:
        task = f"{task}\n\n{chain_note}"
    if seed_urls:
        task = task + "\n\n请优先从以下种子链接收集证据：\n" + "\n".join(seed_urls)

    facts = build_profile_facts(profile)
    needed_skills = tuple(meta.get("skills") or ALL_SKILLS)
    return RunRequest(
        task=task,
        user_id=f"eval-{qid}",
        run_id=f"run-{link_id}",
        allowed_skills=_allowed_skills_for_needed(needed_skills),
        needed_skills=needed_skills,
        budget=BudgetLimits(wall_clock_seconds=wall_clock_seconds),
        seed_artifacts=seed_artifacts,
        private_context={
            "confirmed_profile_facts": facts,
            "candidate_urls": list(seed_urls),
            "search_route_budget": 2,
        },
    )


def _chain_context_note(prev_id: str, prev_summary: str | None) -> str | None:
    """上一环节成果的受控说明（对齐 pi chain.py）。"""
    if not prev_summary:
        return None
    return (
        f"上一环节（{prev_id}）已完成岗位收集；本环节继承其工具产出的来源证据，"
        f"如需补充来源只能使用公开且安全的 URL。上一环节成果参考："
        f"{prev_summary[:_MAX_CHAIN_SUMMARY_CHARS]}"
    )


def _artifact_attr(artifact: Any, name: str, default: Any = None) -> Any:
    """Read a field off a serialized artifact dict or an ``Artifact`` object."""
    if isinstance(artifact, dict):
        return artifact.get(name, default)
    return getattr(artifact, name, default)


def _artifact_content(artifact: Any) -> dict[str, Any]:
    """Serialized artifacts carry ``content_json``; live objects carry ``content``."""
    if isinstance(artifact, dict):
        content = artifact.get("content_json")
        if not isinstance(content, dict):
            content = artifact.get("content")
        return content if isinstance(content, dict) else {}
    return artifact.content or {}


def _inherited_candidate_refs(prev_result: RunResult) -> list[str]:
    """上一环节产物中的引用级 refs（对齐 pi chain.py 的 reference-only 语义）。

    完整 JD 正文只通过 seed artifacts 投影，绝不复制进下游 task。
    """
    refs: list[str] = []
    for artifact in prev_result.artifacts:
        if _artifact_attr(artifact, "artifact_type") == "structured_job_details":
            content = _artifact_content(artifact)
            candidates = content.get("candidates") or content.get("details") or []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                ref = candidate.get("candidate_id") or candidate.get("artifact_id")
                if ref:
                    refs.append(str(ref))
        elif (
            _artifact_attr(artifact, "artifact_type") == "public_job_page"
            and _artifact_attr(artifact, "artifact_id")
        ):
            refs.append(str(_artifact_attr(artifact, "artifact_id")))
        if len(refs) >= 8:
            break
    return refs


def _inherited_goal_supplement(
    prev_result: RunResult | None, profile: dict[str, Any]
) -> str:
    """下游环节 task 的受控补充（对齐 pi chain.py:_inherited_goal_supplement）。

    简历事实是关键：下游 supervisor 必须看到候选人已确认的简历（否则它不知道
    有简历，会转而向用户索要 → waiting_user / completion_evidence_unavailable）。
    """
    lines: list[str] = []
    if prev_result is not None:
        n = 0
        for artifact in prev_result.artifacts:
            if _artifact_attr(artifact, "artifact_type") == "structured_job_details":
                content = _artifact_content(artifact)
                n += len(content.get("candidates") or content.get("details") or [])
            elif _artifact_attr(artifact, "artifact_type") == "public_job_page":
                n += 1
        if n:
            refs = _inherited_candidate_refs(prev_result)
            lines.append(
                "【上一环节已收集的岗位】已持久化 "
                f"{n} 条候选，见 seed artifacts；"
                + ("证据 refs: " + ", ".join(refs) if refs else "")
            )
    facts = build_profile_facts(profile)
    if facts:
        lines.append("【候选人已确认事实（简历）】")
        for key, value in list(facts.items())[:12]:
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _build_seed_artifacts(prev_result: RunResult) -> list[Artifact]:
    """把上一环节产出的 artifacts 重建为种子 Artifact（对齐 pi chain.py）。

    - career_preparation_plan / 其他纯文本型证据 → public_job_page 种子；
    - structured_job_details（candidates 列表）→ structured_job_details 种子。
    """
    return build_seed_artifacts(prev_result)


# ---------------------------------------------------------------------------
# 单题 / 链式运行
# ---------------------------------------------------------------------------


async def _run_one_link(
    controller: CareerRunController,
    *,
    qid: str,
    link_id: str,
    question: str,
    meta: dict[str, Any],
    profile: dict[str, Any],
    seed_urls: list[str],
    seed_artifacts: list[Artifact] | None,
    wall_clock_seconds: int,
    chain_note: str | None,
    goal_supplement: str | None = None,
) -> dict[str, Any]:
    """Run one link and assemble the shared pi_eval_record_v1 link payload."""
    started = time.monotonic()
    request = _build_request(
        qid,
        link_id,
        question,
        meta,
        profile,
        seed_urls=seed_urls,
        seed_artifacts=seed_artifacts,
        wall_clock_seconds=wall_clock_seconds,
        chain_note=chain_note,
        goal_supplement=goal_supplement,
    )
    result = await controller.run(request)
    events: list[dict[str, Any]] = []
    for evt in result.events:
        payload = evt.payload or {}
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 4096:
            payload = {"_payload_truncated": True}
        events.append({"type": evt.type, "payload": payload})

    request = _build_request(
        qid,
        link_id,
        question,
        meta,
        profile,
        seed_urls=seed_urls,
        seed_artifacts=seed_artifacts,
        wall_clock_seconds=wall_clock_seconds,
        chain_note=chain_note,
        goal_supplement=goal_supplement,
    )
    limits = request.budget or BudgetLimits()
    consumed = result.budget
    budget = {
        "limits": {
            "agent_turns": limits.agent_turns,
            "tool_calls": limits.initial_tool_calls,
            "model_requests": limits.model_requests,
            "input_tokens": limits.input_tokens,
            "wall_clock_seconds": limits.wall_clock_seconds,
            "auto_recoveries": limits.auto_recoveries,
        },
        "consumed": {
            "agent_turns": consumed.agent_turns,
            "tool_calls": consumed.tool_calls,
            "model_requests": consumed.model_requests,
            "input_tokens": consumed.input_tokens,
            "wall_clock_seconds": consumed.wall_clock_seconds,
            "auto_recoveries": consumed.auto_recoveries,
        },
    }
    model = controller._model  # noqa: SLF001 - record metadata snapshot
    record: dict[str, Any] = {
        "schema_version": "pi_eval_record_v1",
        "id": link_id,
        "type": "single",
        "question": question,
        "meta": meta,
        "runtime": {"name": "deepagents-skills", "version": "0.1.0"},
        "model": {
            "id": str(
                getattr(model, "model_name", None)
                or getattr(model, "model", None)
                or getattr(model, "id", "")
            ),
            "provider": "deepseek"
            if "deepseek" in str(getattr(model, "base_url", ""))
            else str(getattr(model, "provider", "")),
        },
        "config": {
            "prompt_hashes": dict(PROMPT_HASHES),
            "feature_flags": {"deepagents": True},
            "seeded_urls": list(seed_urls),
        },
        "result": {
            "status": result.status,
            "error_code": result.error_code,
            "summary": result.summary,
        },
        "attempts": [
            {
                "attempt_id": link_id,
                "status": result.status,
                "error_code": result.error_code,
                "summary": result.summary,
                "tool_calls": consumed.tool_calls,
                "events": events,
            }
        ],
        "artifacts": result.artifacts,
        "events": events,
        "budget": budget,
        "audit": {},
        "wall_seconds": round(time.monotonic() - started, 3),
    }
    try:
        record["audit"] = audit_record(
            record,
            inherited_refs=None,
            confirmed_facts=build_profile_facts(profile),
        )
    except Exception:  # noqa: BLE001 - audit is best effort in eval output
        record["audit"] = {
            "status": "inconclusive",
            "reason": "audit_exception",
            "checks": {},
        }
    validate_record(record)
    return record


async def run_question(
    controller: CareerRunController,
    qid: str,
    *,
    question_dir: Path,
    wall_clock_seconds: int = 600,
) -> dict[str, Any]:
    """跑一道题（Q/R 单环节或 C 链式），返回记录 dict。"""
    doc = _load_question(question_dir, qid)
    links: list[dict[str, Any]] = []

    if "chain" in doc:
        # 链式：上一环节 seed 到下一环节
        prev_result: RunResult | None = None
        for idx, link in enumerate(doc["chain"]):
            meta = link.get("meta", {})
            profile = link.get("profile", {})
            # 对齐 pi chain.py：种子 URL 只在第 1 环节注入；后续环节继承
            # seed artifacts + goal supplement（含简历事实），不再重复收集。
            seed_urls = _seeded_urls_for(qid, meta) if idx == 0 else []
            seed_artifacts = (
                _build_seed_artifacts(prev_result) if prev_result is not None else None
            )
            chain_note = (
                _chain_context_note(
                    f"{qid}[{idx - 1}]", prev_result.summary if prev_result else None
                )
                if prev_result is not None
                else None
            )
            goal_supplement = (
                _inherited_goal_supplement(prev_result, profile)
                if prev_result is not None
                else None
            )
            # 链式环节必须串行且复用同一 controller（共享可信内核语义）。
            record = await _run_one_link(
                controller,
                qid=qid,
                link_id=f"{qid}[{idx}]",
                question=link["question"],
                meta=meta,
                profile=profile,
                seed_urls=seed_urls,
                seed_artifacts=seed_artifacts,
                wall_clock_seconds=wall_clock_seconds,
                chain_note=chain_note,
                goal_supplement=goal_supplement,
            )
            links.append(record)
            if record["result"]["status"] != "succeeded":
                break  # 上一环节未成功 → 后续环节不运行（对齐 pi chain）
            # 重建 RunResult 供下一环节 seed
            prev_result = RunResult(
                run_id=f"run-{qid}[{idx}]",
                status=record["result"]["status"],
                summary=record["result"]["summary"],
                error_code=record["result"]["error_code"],
                error_message=None,
                attempt_count=len(record["attempts"]),
                completed_skills=[],
                refs=[],
                artifacts=record["artifacts"],
                events=[],
                budget=BudgetConsumed(),
            )
        last = links[-1]
        record: dict[str, Any] = {
            "schema_version": "pi_eval_record_v1",
            "id": qid,
            "type": "chain",
            "chain_length": len(links),
            "links": [
                {
                    key: link[key]
                    for key in (
                        "id", "question", "meta", "config", "result",
                        "attempts", "artifacts", "events", "budget", "audit",
                        "wall_seconds",
                    )
                }
                for link in links
            ],
            "result": last["result"],
            "audit": {},
            "wall_seconds": round(sum(link["wall_seconds"] for link in links), 3),
        }
        try:
            record["audit"] = audit_chain(
                record,
                confirmed_facts_by_link=[
                    build_profile_facts(doc["chain"][index].get("profile", {}))
                    for index, _link in enumerate(links)
                ],
            )
        except Exception:  # noqa: BLE001 - audit is best effort in eval output
            record["audit"] = {"status": "inconclusive", "reason": "audit_exception"}
        validate_record(record)
        return record

    # 单环节
    meta = doc.get("meta", {})
    profile = doc.get("profile", {})
    seed_urls = _seeded_urls_for(qid, meta)
    record = await _run_one_link(
        controller,
        qid=qid,
        link_id=qid,
        question=doc["question"],
        meta=meta,
        profile=profile,
        seed_urls=seed_urls,
        seed_artifacts=None,
        wall_clock_seconds=wall_clock_seconds,
        chain_note=None,
    )
    return record


def _summary_line(qid: str, record: dict[str, Any]) -> str:
    """一行汇总：状态 / 完成技能 / 产物数。"""
    if record.get("type") == "chain":
        parts = []
        for link in record["links"]:
            parts.append(
                f"{link['id']}:{link['result']['status']}(-"
                f"/{len(link['artifacts'])})"
            )
        return f"{qid:6s} chain -> " + " -> ".join(parts)
    rec = record
    return (
        f"{qid:6s} {rec['result']['status']:12s} "
        f"artifacts={len(rec['artifacts'])} attempts={len(rec['attempts'])}"
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _make_controller(model_id: str) -> CareerRunController:
    if model_id == "faux":
        return CareerRunController(ScriptedFakeChatModel())
    return CareerRunController(create_deepseek_chat_model(model_id))


async def _main() -> int:
    parser = argparse.ArgumentParser(
        description="deepagents 版 25 题回归测试",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--model", default="deepseek-v4-flash",
                        help="模型标识；'faux' 为免 key 冒烟模型")
    parser.add_argument("--question-dir", type=Path, default=DEFAULT_QUESTION_DIR,
                        help="25 题数据集目录")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR,
                        help="结果输出目录")
    parser.add_argument("--qids", nargs="*", default=None,
                        help="只跑指定题（如 --qids Q046 Q017）；默认全部 25 题")
    parser.add_argument("--limit", type=int, default=None,
                        help="最多跑前 N 题（按字母序）")
    parser.add_argument("--wall-clock", type=int, default=600,
                        help="单轮墙钟预算（秒）")
    args = parser.parse_args()

    if not args.question_dir.exists():
        print(f"错误：找不到数据集目录 {args.question_dir}", file=sys.stderr)
        return 2

    qids = args.qids or sorted(
        p.stem for p in args.question_dir.glob("*.json") if p.stem.startswith(("Q", "R", "C"))
    )
    if args.limit is not None:
        qids = qids[: args.limit]

    if args.model != "faux" and not os.environ.get("DEEPSEEK_API_KEY"):
        print(
            "警告：未设置 DEEPSEEK_API_KEY，真实模型运行会直接失败；"
            "可用 --model faux 做免 key 冒烟测试。",
            file=sys.stderr,
        )

    controller = _make_controller(args.model)
    args.out.mkdir(parents=True, exist_ok=True)

    print(f"===== deepagents 25 题回归（model={args.model}, {len(qids)} 题）=====")
    results: list[dict[str, Any]] = []
    for qid in qids:
        started = time.monotonic()
        try:
            record = await run_question(
                controller, qid, question_dir=args.question_dir,
                wall_clock_seconds=args.wall_clock,
            )
        except Exception as exc:  # noqa: BLE001 - 单题失败不影响其余题
            record = {"id": qid, "error": f"{type(exc).__name__}: {exc}"}
        results.append(record)
        print(f"[{time.monotonic() - started:6.1f}s] {_summary_line(qid, record)}")
        (args.out / f"{qid}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print("\n===== 汇总 =====")
    for qid, record in zip(qids, results, strict=True):
        print(_summary_line(qid, record))
    print(f"\n结果已写入: {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
