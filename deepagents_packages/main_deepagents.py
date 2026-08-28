"""deepagents 版单次运行入口（与根目录 ``main.py`` 对齐）。

用户问题 + 简历(URL/PDF/文本) → deepagents 控制器运行结果 + 工具调用日志。

用法（真实运行需要环境变量 DEEPSEEK_API_KEY）：
    .venv/Scripts/python.exe deepagents_packages/main_deepagents.py
或用 ``model_id="faux"`` 做无需 API key 的冒烟测试（脚本内置 ScriptedFakeChatModel，
见 ``deepagents_skills.models``）。

与 pi 版 main.py 的行为差异（结构保持不变，见 MIGRATION.md）：
    - 代理驱动从 pi-agent-core 换成 deepagents ``create_deep_agent``；
    - 委托从 pi delegation tools 换成 deepagents 官方 ``task`` 子代理；
    - harness 逻辑以 langchain AgentMiddleware 的 4 个自定义中间件实现；
    - 其余（简历解析、profile facts、RunRequest 组装、事件 jsonl 日志）完全一致。

工具调用日志：
    - 逐条 print 到控制台；
    - 同时以追加模式写入 ``temp/results/tool_calls.jsonl``（每次运行的行都会累积，
      每行是一条事件，含 run_id/seq/attempt_id 以便区分轮次与运行）。
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests
from deepagents_skills.contracts import RunRequest, RunResult
from deepagents_skills.controller import CareerRunController
from deepagents_skills.models import (
    ScriptedFakeChatModel,
    create_deepseek_chat_model,
)

from pi_career_skills.evaluation.profile_facts import build_profile_facts
from pi_career_skills.evaluation.seed_urls import ALL_SKILLS
from pi_career_skills.runtime.budgets import BudgetLimits

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_LOG_DIR = Path("temp") / "results"
TOOL_LOG_NAME = "tool_calls.jsonl"
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 简历文件大小上限 20MB
DOWNLOAD_TIMEOUT = 30  # 秒


# ---------------------------------------------------------------------------
# 简历加载：URL（支持 .pdf / .txt / .html）或直接文本 → 纯文本
# （与 main.py 完全一致的解析逻辑）
# ---------------------------------------------------------------------------


class _HTMLTextExtractor(HTMLParser):
    """收集 HTML 中所有可见文本（简历页也可能以 HTML 形式给出）。"""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._parts.append(data.strip())

    def text(self) -> str:
        return "\n".join(self._parts)


def _decode_bytes(raw: bytes) -> str:
    """按常见编码顺序解码文本；中文简历常见 GBK/GB18030。"""
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").strip()


def _text_from_pdf_bytes(raw: bytes) -> str:
    """用 pypdf 提取 PDF 文本。"""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 依赖缺失提示
        raise RuntimeError(
            "需要 pypdf 才能解析 PDF 简历，请先运行: uv add pypdf"
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(raw))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise ValueError(f"PDF 解析失败: {exc}") from exc
    text = "\n".join(pages).strip()
    if not text:
        raise ValueError("PDF 未能提取到文本（可能是扫描件，需要 OCR）")
    return text


def _load_resume_text(resume_url: str | None, resume_text: str | None) -> str:
    """简历内容 → 纯文本。

    - 显式传入 ``resume_text`` 时优先使用（已是最终文本）；
    - 否则从 ``resume_url`` 获取：支持本地路径或 http(s) URL，
      按内容自动识别 PDF / HTML / 纯文本。
    """
    if resume_text and resume_text.strip():
        return resume_text.strip()
    if not resume_url or not resume_url.strip():
        raise ValueError("必须提供 resume_url 或 resume_text 之一（matching/tailoring 需要简历事实）")

    url = resume_url.strip()

    # 本地路径
    local = Path(url)
    if local.exists():
        raw = local.read_bytes()
        if raw.startswith(b"%PDF") or local.suffix.lower() == ".pdf":
            return _text_from_pdf_bytes(raw)
        if local.suffix.lower() in {".html", ".htm"}:
            extractor = _HTMLTextExtractor()
            extractor.feed(_decode_bytes(raw))
            return extractor.text()
        return _decode_bytes(raw)

    # http(s) URL —— 带大小上限的流式下载
    resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
    resp.raise_for_status()
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(64 * 1024):
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"简历文件过大（>{MAX_DOWNLOAD_BYTES // 1024 // 1024}MB）")
    raw = b"".join(chunks)

    content_type = (resp.headers.get("content-type") or "").lower()
    if raw.startswith(b"%PDF") or "pdf" in content_type or url.lower().endswith(".pdf"):
        return _text_from_pdf_bytes(raw)
    if "html" in content_type or url.lower().endswith((".html", ".htm")):
        extractor = _HTMLTextExtractor()
        extractor.feed(_decode_bytes(raw))
        return extractor.text()
    return _decode_bytes(raw)


# ---------------------------------------------------------------------------
# 工具调用日志：print + jsonl 追加
# ---------------------------------------------------------------------------


def _format_event(event: Any) -> str:
    """把一条 RunEvent 格式化为可读的一行日志。"""
    p = event.payload
    t = event.type
    if t == "run_started":
        return f"[{t}] user={p.get('user_id')} skills={p.get('needed_skills')}"
    if t == "attempt_started":
        return f"[{t}] 第 {p.get('attempt_index', 0) + 1} 轮"
    if t == "tool_observation":
        detail = f" error={p.get('error_code') or ''}".rstrip()
        return (
            f"[tool] {p.get('tool_name')} -> {p.get('status')} "
            f"(artifacts={p.get('promoted_artifacts', 0)}){detail}"
        )
    if t.startswith("delegation_"):
        status = t.removeprefix("delegation_")
        code = p.get("error_code") or ""
        return f"[delegate] {status} skill={p.get('skill')} {code}".rstrip()
    if t == "attempt_finished":
        code = p.get("error_code") or ""
        return f"[{t}] 第 {p.get('attempt_index', 0) + 1} 轮 -> {p.get('status')} {code}".rstrip()
    if t == "run_finalized":
        return f"[{t}] status={p.get('status')} completed={p.get('completed_skills')}"
    if t == "stall_soft_warning":
        return f"[stall] kind={p.get('kind')} streak={p.get('streak')}"
    return f"[{t}] {json.dumps(p, ensure_ascii=False)}"


def log_run_events(result: RunResult, log_path: Path) -> None:
    """打印全部事件，并把每条事件追加写入 jsonl（随时添加，不覆盖）。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        for event in result.events:
            line = {
                "ts": datetime.now(UTC).isoformat(),
                "seq": event.seq,
                "type": event.type,
                "run_id": event.run_id,
                "attempt_id": event.attempt_id,
                "payload": event.payload,
            }
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            fh.flush()  # 随时添加：每条立即落盘
            print(_format_event(event))


# ---------------------------------------------------------------------------
# 控制器构建
# ---------------------------------------------------------------------------


def _make_controller(model_id: str) -> CareerRunController:
    """按 model_id 构建控制器；\"faux\" 走假模型（无需 API key）。"""
    if model_id == "faux":
        return CareerRunController(ScriptedFakeChatModel())
    return CareerRunController(create_deepseek_chat_model(model_id))


# ---------------------------------------------------------------------------
# 主异步函数
# ---------------------------------------------------------------------------


async def run_career_task(
    question: str,
    resume_url: str | None = None,
    resume_text: str | None = None,
    *,
    user_id: str = "app-user",
    model_id: str = "deepseek-v4-flash",
    skills: list[str] | None = None,
    wall_clock_seconds: int = 600,
    log_dir: str | Path = DEFAULT_LOG_DIR,
) -> RunResult:
    """跑一次求职多 Agent 任务（deepagents 版）。

    参数：
        question: 用户问题（如“收集华为 AI 应用开发岗位并匹配我的简历”）。
        resume_url: 简历地址，支持 http(s) URL 或本地路径；.pdf/.txt/.html 自动识别。
        resume_text: 简历纯文本；与 resume_url 二选一，显式提供时优先。
        user_id: 运行所属用户（控制器必需，默认 app-user）。
        model_id: 模型标识（默认 deepseek-v4-flash；\"faux\" 为免 key 冒烟模型）。
        skills: 允许/需要的技能范围，默认全部四个技能。
        wall_clock_seconds: 单轮墙钟预算（秒）。
        log_dir: 工具调用日志目录（默认 temp/results，文件为 tool_calls.jsonl）。

    返回：
        RunResult —— 终态、摘要、证据库 artifacts、完整事件流等。

    环境变量：
        DEEPSEEK_API_KEY —— 真实模型运行必需，缺失时控制器直接返回失败。
    """
    log_path = Path(log_dir) / TOOL_LOG_NAME

    # 1. 简历 → 纯文本 → 已确认简历事实（matching/tailoring 的输入契约）
    resume = _load_resume_text(resume_url, resume_text)
    facts = build_profile_facts({"resume_text": resume})

    # 2. 组装 RunRequest（对齐 evaluation/runner.py 的构建方式）
    needed = tuple(skills) if skills else tuple(ALL_SKILLS)
    request = RunRequest(
        task=question,
        user_id=user_id,
        allowed_skills=needed,
        needed_skills=needed,
        budget=BudgetLimits(wall_clock_seconds=wall_clock_seconds),
        private_context={
            "confirmed_profile_facts": facts,
            # 搜索路由保持有界：聚合站是兜底来源，不是无限制展开的许可。
            "search_route_budget": 2,
        },
    )

    # 3. 运行可信内核（模型输出不被信任，完成与否由完成门+证据库判定）
    controller = _make_controller(model_id)
    result = await controller.run(request)

    # 4. 工具调用日志：print + jsonl 追加
    print(f"\n===== 工具调用日志（run_id={result.run_id}）=====")
    log_run_events(result, log_path)
    print(f"日志已追加写入: {log_path.resolve()}")

    # 5. 最终结果
    print("\n===== 最终结果 =====")
    print(f"status: {result.status}")
    if result.error_code:
        print(f"error_code: {result.error_code}")
    if result.error_message:
        print(f"error_message: {result.error_message}")
    print(f"completed_skills: {result.completed_skills}")
    print(f"attempts: {result.attempt_count}  budget: {result.budget}")
    if result.summary:
        print(f"\nsummary:\n{result.summary}")
    if result.artifacts:
        print("\n--- artifacts ---")
        for art in result.artifacts:
            print(
                f"- {art.get('artifact_type')} [{art.get('artifact_id')}] "
                f"url={art.get('source_url')}"
            )
    return result


# ---------------------------------------------------------------------------
# 命令行示例
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    # 真实运行示例：Q046 题目（25 题之一）+ 本地 PDF 简历 + DeepSeek。
    # 需要环境变量 DEEPSEEK_API_KEY；模型默认 deepseek-v4-flash。
    _QUESTION = (
        "目标岗位限定为 AI应用开发、Agent开发等岗位。收集腾讯智能文档里最近2天（8月27号~8月28号）相关的岗位"
        "按匹配度筛选。"
    )
    _SEED_URL = (
        "https://www.iguopin.com/job/list?keyword=AI%E5%BA%94%E7%94%A8%E5%BC%80%E5%8F%91"
    )
    _RESUME_PDF = "D:\\Desktop\\高硕谦+东北大学+控制科学与工程+硕士.pdf"

    result = asyncio.run(
        run_career_task(
            # question=_QUESTION + "\n\n请优先从以下种子链接收集证据：\n" + _SEED_URL,
            question=_QUESTION,
            resume_url=_RESUME_PDF,
            user_id="grefer",
            skills=["job-discovery", "job-matching"],
            wall_clock_seconds=900,
        )
    )
