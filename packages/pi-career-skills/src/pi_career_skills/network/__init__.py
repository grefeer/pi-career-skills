"""Network tool layer for the ``job-discovery`` Skill (Phase 6).

The public tool handlers are implemented in focused modules:

- :mod:`.url_guard` — SSRF guard (scheme / userinfo / DNS global-IP checks);
- :mod:`.page_fetch` — single-page fetch (requests path + evidence building);
- :mod:`.batch_fetch` — batch fetch with list expansion and portal discovery;
- :mod:`.public_search` — Bing -> 360 -> Sogou chain + Juejin official search;
- :mod:`.classify_url` — 4KB probe site classification;
- :mod:`.career_sheets` — Tencent smartsheet records via the mcporter bridge;
- :mod:`.wechat` / :mod:`.wechat_slice` / :mod:`.extract_gate` — WeChat OCR
  pipeline and the regex-first extraction gate;
- :mod:`.playwright_worker` / :mod:`.subprocess_runner` / :mod:`.page_links` —
  render fallback, script running and link collection.
"""

from .batch_fetch import fetch_public_job_pages
from .career_sheets import query_career_sheet_records
from .classify_url import classify_job_url
from .page_fetch import fetch_public_job_page
from .public_search import search_public_job_pages
from .wechat import fetch_wechat_article

__all__ = [
    "fetch_public_job_pages",
    "fetch_public_job_page",
    "search_public_job_pages",
    "query_career_sheet_records",
    "fetch_wechat_article",
    "classify_job_url",
]
