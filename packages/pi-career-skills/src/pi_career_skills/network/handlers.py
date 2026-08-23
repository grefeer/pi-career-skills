"""Network tool handlers — real Phase 6 implementations.

Phase 6 replaces the fail-closed stubs with implementations ported verbatim
from ``skill/job_discovery/runtime/*``:

- ``fetch_public_job_pages``  -> :mod:`.batch_fetch` (list expansion + official
  campus / iguopin / tencent detail discovery);
- ``fetch_public_job_page``   -> :mod:`.page_fetch` (SSRF-guarded requests path
  + Playwright render fallback + WeChat OCR route);
- ``search_public_job_pages`` -> :mod:`.public_search` (Bing -> 360 -> Sogou
  fallback chain + Juejin official recent search);
- ``query_career_sheet_records`` -> :mod:`.career_sheets` (mcporter bridge with
  stable ``SheetQueryError`` codes);
- ``fetch_wechat_article``    -> :mod:`.wechat` (OCR pipeline, gated off by
  default);
- ``classify_job_url``        -> :mod:`.classify_url` (4KB probe site
  classification).

Signatures follow the shared handler contract ``(context, input) -> output``,
so the registry wiring is unchanged from the fail-closed stubs.
"""

from __future__ import annotations

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
