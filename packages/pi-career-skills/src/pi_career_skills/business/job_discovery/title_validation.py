"""Title helpers for job-discovery — VERBATIM port from the source project.

The three functions below are ported word-for-word (same logic, same
constants, same regexes) from
``skill/job_discovery/runtime/job_discovery.py`` in the source project
(``_CAMPUS_PORTAL_HOST`` at line 79, ``_extract_portal_role_text`` at
lines 3514-3527, ``_infer_official_page_title`` at lines 3530-3562,
``_is_plausible_job_title`` at lines 3565-3599).  Only the module layout
differs: they live in their own module so the business handlers stay under
the project's line-count cap.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_CAMPUS_PORTAL_HOST = "career.hebut.edu.cn"


def _extract_portal_role_text(text: str, source_url: str) -> str:
    """Keep official portal role-family lines that identify specific roles."""
    parsed = urlsplit(source_url)
    if (
        (parsed.hostname or "").lower() != _CAMPUS_PORTAL_HOST
        or "/correcruit/content/" not in parsed.path.lower()
    ):
        return ""
    match = re.search(
        r"职位类型：\s*(.*?)\s*(?=招聘流程：|工作地点：|投递简历|$)",
        text,
        flags=re.DOTALL,
    )
    return " ".join(match.group(1).split()).strip() if match else ""


def _infer_official_page_title(text: str) -> str | None:
    """Infer a title from the header area of official pages lacking a title label.

    Uses frequency voting over header lines so a short real title (which a
    page repeats in its H1 / breadcrumb / og:title / meta description) beats a
    one-off navigation label such as "浏览职位" or a long body sentence.
    """
    header = re.split(
        r"(?:岗位职责|工作职责|职位描述|关于这个职位)\s*[:：]?", text, maxsplit=1
    )[0]
    role_pattern = re.compile(
        r"(?:工程师|开发|算法|研究员|实习生|架构师|科学家|产品经理|项目经理|"
        r"专家|设计师|分析师|管培生)"
    )
    noise_pattern = re.compile(
        r"申请|浏览|查看|立即应聘|免费试用|购买|订阅|登录|注册|首页|联系|关于"
    )
    tallies: dict[str, int] = {}
    for line in header.splitlines():
        candidate = " ".join(line.split()).strip()
        if not (3 <= len(candidate) <= 40):
            continue
        if not role_pattern.search(candidate):
            continue
        if noise_pattern.search(candidate):
            continue
        tallies[candidate] = tallies.get(candidate, 0) + 1
    if not tallies:
        return None
    return max(
        tallies.items(),
        key=lambda item: (item[1], len(role_pattern.findall(item[0]))),
    )[0]


def _is_plausible_job_title(value: object) -> bool:
    """Reject page chrome or numbered safety notes as a job title."""
    if not isinstance(value, str):
        return False
    candidate = " ".join(value.split()).strip()
    if not 2 <= len(candidate) <= 80:
        return False
    if re.match(r"^\d+[.、)]", candidate):
        return False
    return not any(
        marker in candidate
        for marker in (
            "如您应聘",
            "温馨提示",
            "平台内招聘方",
            "安全防范",
            "举报",
            "查看全部",
            "浏览职位",
            "立即应聘",
            "查看详情",
            "职位信息",
            "职位点评",
            "申请职位",
            "招聘观察",
            "更多筛选",
            "职位收藏",
            "职位订阅",
            "热门职位",
            "最新职位",
            "推荐职位",
            "共条职位",
            "共个职位",
        )
    )
