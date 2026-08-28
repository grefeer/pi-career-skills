"""Network tool layer for the ``job-discovery`` Skill (Phase 6).

The six tool handlers live in :mod:`.tool_handlers` (thin re-exports keeping
the registry wiring stable); their implementations are split across:

- :mod:`.url_guard` — SSRF guard (scheme / userinfo / DNS global-IP checks);
- :mod:`.page_fetch` — single-page fetch (requests path + evidence building);
- :mod:`.batch_fetch` — batch fetch with list expansion and portal discovery;
- :mod:`.public_search` — Bing -> 360 -> Sogou chain + Juejin official search;
- :mod:`.classify_url` — 4KB probe site classification;
- :mod:`.career_sheets` — Tencent smartsheet records via the mcporter bridge;
- :mod:`.wechat` / :mod:`.wechat_slice` / :mod:`.extract_gate` — WeChat OCR
  pipeline (gated off by default) and the regex-first extraction gate;
- :mod:`.playwright_worker` / :mod:`.subprocess_runner` / :mod:`.page_links` —
  render fallback, script running and link collection.
"""

from .tool_handlers import (
    classify_job_url,
    fetch_public_job_page,
    fetch_public_job_pages,
    fetch_wechat_article,
    query_career_sheet_records,
    search_public_job_pages,
)

__all__ = [
    "fetch_public_job_pages",
    "fetch_public_job_page",
    "search_public_job_pages",
    "query_career_sheet_records",
    "fetch_wechat_article",
    "classify_job_url",
]
