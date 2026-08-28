"""Best-effort partial answers for non-succeeded runs (waiting_user / failed).

When a run ends without satisfying the completion gates — budget exhausted,
wall-clock killed, evidence below the bar, anti-bot handoff — the user still
deserves the evidence that *was* collected.  ``build_partial_answer`` renders
a deterministic, bounded, source-backed text answer from the store's persisted
artifacts, so ``summary`` is never empty just because the supervisor was
killed mid-flight.

The renderer is part of the trusted kernel: it reads only persisted artifacts
(never model output), and every listed lead carries its apply/source URL, so a
partial answer cannot invent jobs that were never observed.
"""

from __future__ import annotations

from typing import Any

_STATUS_NOTES: dict[str, str] = {
    "budget_exhausted": "模型调用预算耗尽，本轮自动终止",
    "wall_clock_budget_exhausted": "运行墙钟超时，被后备机制终止",
    "completion_evidence_unavailable": "已收集证据未达到完成门槛（缺少完整 JD 正文）",
    "no_progress": "多次尝试未取得实质进展",
    "anti_bot_challenge": "公共来源触发反爬/人工核验，需要人工确认",
    "auto_recovery_limit_reached": "自动恢复达到上限，已停止重试",
    "route_already_consumed": "搜索路由已用完",
    "delegation_retry_limit": "子代理委托重试达到上限",
    "invalid_model_response": "模型返回了无效响应",
}
_DEFAULT_NOTE = "运行未完成（预算/证据/来源限制）"

_MAX_ARTIFACTS = 12  # 参与渲染的产物上限
_MAX_LEADS = 20  # 列出的岗位线索上限
_MAX_BODY_CHARS = 3_500  # 渲染正文硬上限（对齐 _bounded_summary 的量级）
_SUMMARY_CHARS = 120  # 单条摘要截断


def _error_note(error_code: str | None) -> str:
    if error_code in _STATUS_NOTES:
        return _STATUS_NOTES[error_code]
    return _DEFAULT_NOTE


def _record_label(record: dict[str, Any]) -> str | None:
    """Render one sheet/search record line, or ``None`` when it has no link.

    Sheet records (``query-career-sheet-records``) and search results
    (``search-public-job-pages``) share the same persistence shape: a company/
    title, an apply URL, and optional industry/location/type/time metadata.
    """
    url = record.get("apply_url") or record.get("url") or ""
    if not url:
        return None
    company = record.get("company_name") or record.get("title") or ""
    bits = [str(company)] if company else []
    industry = record.get("industry") or ""
    location = record.get("location") or ""
    if industry or location:
        bits.append("/".join(str(x) for x in (industry, location) if x))
    rtype = record.get("recruitment_type") or ""
    if rtype:
        bits.append(str(rtype))
    updated = record.get("updated_at") or record.get("published_at") or ""
    if updated:
        bits.append(f"更新 {updated}")
    prior = record.get("prior_metadata")
    if isinstance(prior, dict):
        code = prior.get("referral_code")
        if code:
            bits.append(f"内推码 {code}")
    summary = record.get("raw_summary") or record.get("snippet") or ""
    line = "｜".join(bits) if bits else url
    if summary:
        line = f"{line}\n    摘要: {str(summary)[:_SUMMARY_CHARS]}"
    return f"- {line}\n    投递: {url}"


def _jd_label(content: dict[str, Any], url: str | None) -> str | None:
    """Render one structured JD / public page artifact line."""
    title = content.get("title") or content.get("job_title")
    company = content.get("company_name") or content.get("company")
    parts: list[str] = []
    if title:
        parts.append(str(title))
    if company:
        parts.append(str(company))
    locs = content.get("locations")
    if isinstance(locs, list) and locs:
        parts.append("/".join(str(x) for x in locs[:2]))
    line = "｜".join(parts) if parts else None
    if not line and not url:
        return None
    link = f" 投递: {url}" if url else ""
    return f"- {line if line else '岗位详情'}{link}"


def build_partial_answer(store: Any, *, error_code: str | None = None) -> str | None:
    """Render a bounded, source-backed partial answer from stored artifacts.

    Returns ``None`` when the store holds no renderable evidence (the caller
    then keeps whatever summary already exists).  The output is deterministic
    and cites each lead's source URL — it is never model-generated.
    """
    # Union of usable evidence (incl. non-job-bearing sheet/search results)
    # and job-bearing deliverables, deduped by artifact_id in insertion order.
    seen: dict[str, Any] = {}
    for artifact in store.usable_evidence_artifacts():
        seen.setdefault(artifact.artifact_id, artifact)
    for artifact in store.job_bearing_artifacts():
        seen.setdefault(artifact.artifact_id, artifact)
    artifacts = list(seen.values())

    lead_lines: list[str] = []
    lead_count = 0
    for artifact in artifacts[:_MAX_ARTIFACTS]:
        content = artifact.content or {}
        if artifact.artifact_type == "job_search_results":
            for key in ("records", "results"):
                items = content.get(key)
                if not isinstance(items, list):
                    continue
                for record in items:
                    if not isinstance(record, dict):
                        continue
                    label = _record_label(record)
                    if label is None:
                        continue
                    lead_lines.append(label)
                    lead_count += 1
                    if lead_count >= _MAX_LEADS:
                        break
                if lead_count >= _MAX_LEADS:
                    break
            if lead_count >= _MAX_LEADS:
                break
        elif artifact.artifact_type in {"public_job_page", "structured_job_details"}:
            label = _jd_label(content, artifact.source_url)
            if label is not None:
                lead_lines.append(label)
                lead_count += 1

    if not lead_lines:
        return None

    header = (
        f"⚠️ {_error_note(error_code)}，未能按完成门槛交付。"
        f"以下为运行终止时已收集到的证据（{lead_count} 条岗位线索，部分完成）："
    )
    body = "\n".join(lead_lines)
    if len(body) > _MAX_BODY_CHARS:
        body = body[:_MAX_BODY_CHARS].rstrip() + "\n…（已截断）"
    footer = (
        "\n\n说明：以上为已持久化的中间证据，未完成匹配度筛选，也未必包含完整 JD 正文。"
        "可点击上方投递链接继续查看；如需完整结论，请调整题目或放宽关键词/时间范围后重试。"
    )
    return f"{header}\n{body}{footer}"


__all__ = ["build_partial_answer"]
