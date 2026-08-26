"""One-shot Playwright render worker + bounded fallback renderer + browse automation.

Ports of ``skill/job_discovery/runtime/playwright_worker.py`` and the external
``scripts/browse.py`` automation primitives, consolidated into one shared
browser kernel:

- ``_render`` / ``main`` (worker subprocess, one-shot payload JSON);
- ``_render_metadata`` / ``_set_render_metadata`` (thread-local, tuple-API compat);
- ``_playwright_worker_command`` / ``_render_with_playwright_process`` -- the
  killable child process with the hard per-mode deadline;
- ``enable_playwright_fallback`` / ``configure_playwright_storage_state`` --
  Playwright is OFF by default and never loads a login profile;
- ``_render_with_playwright`` -- route-guarded render, one relaunch-once
  policy, threaded watchdog bounding (unchanged tuple contract);
- ``_browse_with_playwright`` / ``_browse_once`` -- the P0-P2 automation kernel:
  consent dismissal with security hard gates, aggressive load-all scrolling,
  URL-pattern pagination, card click-through, in-site search, and steering
  signals (cards_visible / estimated_total_items / pagination_pattern / ...);
- module-level bounded TTL render cache (P1-2).

The worker import stays lazy/guarded so this package works without the
optional ``playwright`` extra installed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from ..business.job_discovery.models import _MIN_USABLE_TEXT_CHARS
from .page_links import _collect_page_links
from .request_governor import canonical_request_url
from .subprocess_runner import _terminate_process_tree
from .url_guard import PublicFetchError, _assert_public_url, _is_public_url

#: Codes a successful requests fetch may still hand to the render fallback
#: (an empty / shell / fetch-failed page is worth one render attempt).
_PLAYWRIGHT_FALLBACK_CODES = frozenset(
    {"public_fetch_failed", "empty_public_page", "public_page_content_insufficient"}
)

#: Playwright fallback is OFF unless runtime assembly opts in.
_PLAYWRIGHT_FALLBACK_ENABLED = False
#: Compatibility reset seam only. Phase 6 never loads a profile.
_PLAYWRIGHT_STORAGE_STATE_PATH: str | None = None

_RENDER_LOCK = threading.Lock()
_RENDER_TIMEOUT_S = 60

#: Unit-test seam: when set, ``_render_with_playwright`` calls it directly
#: instead of launching any browser or subprocess.
_PLAYWRIGHT_FETCH_IMPL: Callable[[str], tuple[str, str | None]] | None = None
#: (sync_playwright, browser) shared runtime; one relaunch per process.
_PLAYWRIGHT_RUNTIME: tuple[Any, Any] | None = None
_RENDER_METADATA = threading.local()
#: Full payload of the most recent browser op (tuple-API renders still expose
#: the richer browse signals through here).
_RENDER_PAYLOAD = threading.local()

#: Per-mode hard deadline for a child-process / watchdog-bounded render.  The
#: default fetch fallback keeps the historical 60s; card click-through may
#: legitimately need longer (the ported routine runs up to 2 minutes).
_MODE_TIMEOUT_S = {
    "render": 60,
    "load-all": 75,
    "paginate": 90,
    "interact": 150,
    "search": 60,
}

#: Module-level bounded render cache (P1-2).  Process-scoped by design: the
#: cross-run persistence model stays a documented, deliberately-deferred P3.
#: Only the real child-process path caches (seams/fakes stay deterministic).
_RENDER_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_RENDER_CACHE_MAX = 64
_RENDER_CACHE_TTL_S = 6 * 60 * 60


# ---------------------------------------------------------------------------
# Consent dismissal (security hard gates)
# ---------------------------------------------------------------------------

_CONSENT_BUTTON_TEXTS = [
    "Accept", "Accept All", "Accept all", "Allow", "Agree",
    "同意", "接受", "允许", "我知道了", "确定", "好的", "知道了",
    "I agree", "OK", "Ok", "Got it", "Continue",
]

# Security hard gate: never click consent if page body contains any of these.
# These indicate login walls, captcha, anti-bot, or WeChat verification —
# clicking through would be circumventing a security measure.
_CONSENT_BLOCK_KEYWORDS = [
    "验证码", "captcha", "滑块", "登录", "扫码", "robot",
    "环境异常", "完成验证后即可继续访问",
    "请在微信客户端打开", "请长按识别二维码",
]


def _dismiss_consent(page: Any) -> bool:
    try:
        body_text = page.evaluate("() => document.body.innerText || ''")
        body_lower = body_text.lower()
        for kw in _CONSENT_BLOCK_KEYWORDS:
            if kw.lower() in body_lower:
                return False  # Security hard gate — do NOT interact
    except Exception:
        pass

    for text in _CONSENT_BUTTON_TEXTS:
        try:
            btn = page.get_by_text(text, exact=True).first
            if btn.is_visible():
                btn_text = btn.inner_text().lower()
                # Double-check: the button itself must not be a block keyword
                for kw in _CONSENT_BLOCK_KEYWORDS:
                    if kw.lower() in btn_text:
                        return False
                btn.click(timeout=3000)
                page.wait_for_timeout(1000)
                return True
        except Exception:
            continue
    return False


def _extract_body_text(page: Any) -> str:
    """Body innerText via JS, with a plain-API fallback for minimal fakes."""
    try:
        return page.evaluate("() => document.body.innerText || ''")
    except Exception:
        pass
    try:
        return page.inner_text("body") or ""
    except Exception:
        return ""


def _scroll_to_load(page: Any, wait_ms: int = 2000, rounds: int = 3) -> None:
    for _ in range(rounds):
        with suppress(Exception):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(wait_ms)
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=5000)


# ---------------------------------------------------------------------------
# Load-all scrolling (load-more / show-all buttons + aggressive scroll)
# ---------------------------------------------------------------------------

_LOAD_MORE_TEXTS = [
    "加载更多", "查看更多", "Load more", "Show more",
    "展开更多", "显示更多", "继续加载", "点击加载更多",
]

_SHOW_ALL_TEXTS = [
    "查看全部", "全部", "Show all", "View all", "展开全部",
    "查看所有", "全部职位", "All jobs",
]


def _try_load_all_scroll(page: Any, wait_ms: int, max_rounds: int = 8) -> tuple[bool, int]:
    """Aggressively scroll to trigger lazy loading / infinite scroll.

    Also clicks "show all" (round 0) and "load more" buttons when found.
    Returns (got_more_content, total_rounds).
    """
    prev_text_len = len(_extract_body_text(page))
    prev_card_count = _count_job_cards(page)
    rounds_no_change = 0
    total_rounds = 0
    got_more = False

    for rnd in range(max_rounds):
        total_rounds = rnd + 1
        # Try clicking "show all" first (if found)
        if rnd == 0:
            for text in _SHOW_ALL_TEXTS:
                try:
                    btn = page.get_by_text(text).first
                    if btn.is_visible():
                        btn.click(timeout=3000)
                        page.wait_for_timeout(wait_ms)
                        with suppress(Exception):
                            page.wait_for_load_state("networkidle", timeout=10000)
                        break
                except Exception:
                    continue

        # Scroll to bottom
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(min(wait_ms, 1500))

        # Try clicking "load more" buttons
        for text in _LOAD_MORE_TEXTS:
            try:
                btn = page.get_by_text(text).first
                if btn.is_visible():
                    btn.click(timeout=3000)
                    page.wait_for_timeout(wait_ms)
                    break
            except Exception:
                continue

        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=5000)

        # Check if new content appeared
        new_text_len = len(_extract_body_text(page))
        new_card_count = _count_job_cards(page)

        if new_card_count > prev_card_count or new_text_len > prev_text_len + 200:
            got_more = True
            prev_text_len = new_text_len
            prev_card_count = new_card_count
            rounds_no_change = 0
        else:
            rounds_no_change += 1
            if rounds_no_change >= 3:
                break  # Three consecutive rounds with no new content → done

    return got_more, total_rounds


# ---------------------------------------------------------------------------
# URL-pattern pagination (query / offset / path)
# ---------------------------------------------------------------------------

_PAGE_SIZE_PARAMS = ["pageSize", "limit", "size", "per_page", "page_size", "count", "pageSize"]
_PAGE_NUM_PARAMS = ["page", "p", "pageNum", "page_num", "pageNo", "pageIndex", "currentPage"]
_OFFSET_PARAMS = ["offset", "start", "skip"]


def _detect_url_page_pattern(url: str) -> dict[str, Any] | None:
    """Detect if the URL supports page-number or offset-based pagination.

    Returns a dict with pattern info, or None if no recognizable pattern.

    Examples:
      "?page=2"              → {"type": "query", "param": "page", "page": 2}
      "?offset=20&limit=10"  → {"type": "offset", "param": "offset", "page_size": 10}
      "/page/3/"             → {"type": "path", "pattern": "/page/{page}/"}
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)

    # Detect page-number query param
    for pnp in _PAGE_NUM_PARAMS:
        if pnp in query:
            try:
                page_num = int(query[pnp][0])
                return {"type": "query", "param": pnp, "page": page_num}
            except (ValueError, IndexError):
                pass

    # Detect offset-based pagination
    for op in _OFFSET_PARAMS:
        if op in query:
            try:
                offset = int(query[op][0])
                page_size = 10  # Default assumption
                for psp in _PAGE_SIZE_PARAMS:
                    if psp in query:
                        with suppress(ValueError, IndexError):
                            page_size = int(query[psp][0])
                        break
                return {
                    "type": "offset", "param": op,
                    "offset": offset, "page_size": page_size,
                }
            except (ValueError, IndexError):
                pass

    # Detect path-based pagination (e.g., /page/2/ or /jobs?page=2)
    path_match = re.search(r"/page/(\d+)", parsed.path)
    if path_match:
        return {
            "type": "path",
            "pattern": re.sub(r"/page/\d+", "/page/{page}", parsed.path),
            "page": int(path_match.group(1)),
        }

    return None


def _build_page_url(base_url: str, pattern: dict[str, Any], target_page: int) -> str:
    """Construct a URL for a specific page number using the detected pattern."""
    parsed = urlparse(base_url)

    if pattern["type"] == "query":
        query = parse_qs(parsed.query, keep_blank_values=True)
        query[pattern["param"]] = [str(target_page)]
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    if pattern["type"] == "offset":
        query = parse_qs(parsed.query, keep_blank_values=True)
        page_size = pattern.get("page_size", 10)
        query[pattern["param"]] = [str((target_page - 1) * page_size)]
        for psp in _PAGE_SIZE_PARAMS:
            if psp in query:
                query[psp] = [str(page_size)]
                break
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    if pattern["type"] == "path":
        new_path = pattern["pattern"].replace("{page}", str(target_page))
        return urlunparse(parsed._replace(path=new_path))

    return base_url


# ---------------------------------------------------------------------------
# Steering signals (cards_visible / estimated_total_items)
# ---------------------------------------------------------------------------

_JOB_CARD_SELECTORS_JS = """
() => {
    const patterns = [
        '[class*="job-card"]', '[class*="JobCard"]', '[class*="job-item"]',
        '[class*="position"]', '[class*="card"] li', '.job-list > *',
        '[class*="list"] > li', 'a[href*="job"]', 'a[href*="position"]',
    ];
    const seen = new Set();
    for (const sel of patterns) {
        try {
            const els = document.querySelectorAll(sel);
            for (const el of els) {
                if (seen.has(el)) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    seen.add(el);
                }
            }
        } catch(e) {}
        if (seen.size > 5) break;
    }
    return Math.min(seen.size, 200);
}
"""


def _count_job_cards(page: Any) -> int:
    """Estimate the number of visible job cards/positions on the page.

    Uses JavaScript with Set-based element deduplication so a card matching
    multiple selectors is only counted once. Caps at 200 to avoid runaway
    counting on huge pages. Used to detect whether a search actually narrowed
    results vs client-side fake filtering (where all cards remain in the DOM).
    """
    try:
        return page.evaluate(_JOB_CARD_SELECTORS_JS)
    except Exception:
        return 0


def _estimate_total_items(page: Any) -> int | None:
    """Try to read total item count from page text.

    Looks for patterns like:
      - "共 500 个职位"
      - "1-20 of 500 jobs"
      - "共500条"
    """
    body = _extract_body_text(page)

    # Chinese patterns
    for pat in [
        r"共\s*(\d+)\s*个?职?位",
        r"共\s*(\d+)\s*条",
        r"共计\s*(\d+)\s*个",
        r"找到\s*(\d+)\s*个?职?位",
    ]:
        m = re.search(pat, body)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass

    # English patterns
    for pat in [
        r"(\d+)\s+jobs?\s+found",
        r"of\s+(\d+)\s+jobs",
        r"Showing\s+\d+\-\d+\s+of\s+(\d+)",
    ]:
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass

    return None


# ---------------------------------------------------------------------------
# In-site search (search-job-site)
# ---------------------------------------------------------------------------

# Priority-ordered selectors for search input fields across common career platforms.
_SEARCH_INPUT_SELECTORS = [
    # Moka-style
    "input[placeholder*='搜索职位']",
    "input[placeholder*='搜索岗位']",
    # zhiye.com
    "input[placeholder*='请输入职位']",
    "input[placeholder*='请输入岗位']",
    "input[placeholder*='职位名称']",
    "input[placeholder*='岗位名称']",
    # Feishu / generic
    "input[placeholder*='Search']",
    "input[placeholder*='search']",
    "input[placeholder*='搜索']",
    "input[placeholder*='关键词']",
    "input[placeholder*='关键字']",
    # Semantic / aria
    "input[aria-label*='search']",
    "input[aria-label*='搜索']",
    "input[type='search']",
    # CSS class patterns
    "[class*='search'] input[type='text']",
    "[class*='Search'] input[type='text']",
    "[class*='search-input']",
    ".ant-input-search input",
    ".el-input__inner[placeholder*='搜索']",
    # Broad last-resort fallback — only matched if nothing above worked
    "input[type='text']:not([placeholder*='邮箱']):not([placeholder*='手机']):not([placeholder*='电话'])",
]

# Priority-ordered selectors for search submit buttons.
_SEARCH_BUTTON_SELECTORS = [
    "button:has-text('搜索')",
    "button:has-text('Search')",
    "a:has-text('搜索')",
    "[aria-label*='搜索']",
    "[aria-label*='search']",
    ".search-btn", ".search-button",
    "[class*='search'] button",
    "button[type='submit']",
]

# Results-count indicators for validating that search actually filtered.
_RESULT_COUNT_SELECTORS = [
    "[class*='result-count']", "[class*='total']",
    "[class*='count']", "[class*='Count']",
    "span:has-text('个职位')", "span:has-text('条结果')",
    "div:has-text('个职位')", ":has-text('个职位')",
]

_SEARCH_TYPE_DELAY_MS = 100


def _find_search_input(page: Any) -> Any | None:
    """Find a visible search input element on the page."""
    for sel in _SEARCH_INPUT_SELECTORS:
        try:
            elements = page.locator(sel).all()
            for el in elements:
                try:
                    if not el.is_visible():
                        continue
                    # For the broad fallback selector, apply size heuristic
                    box = el.bounding_box()
                    if box and box["width"] < 100:
                        continue  # Too narrow to be a search box
                    return el
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _find_search_button(page: Any) -> Any | None:
    """Find a visible search submit button near the search input."""
    for sel in _SEARCH_BUTTON_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible():
                return btn
        except Exception:
            continue
    return None


def _read_result_count_text(page: Any) -> str | None:
    """Try to read a result-count indicator (e.g. '共 42 个职位')."""
    for sel in _RESULT_COUNT_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.is_visible():
                text = el.inner_text()
                if text and len(text) < 50:
                    return text.strip()
        except Exception:
            continue
    return None


def _perform_search(page: Any, term: str, wait_ms: int) -> tuple[bool, str]:
    """Locate the search box, enter a keyword, and trigger the search.

    Tries three trigger strategies in order:
      1. Press Enter (works for most sites)
      2. Click a visible search button
      3. Type and wait (real-time filtering without explicit submit)

    Returns (success, details_string).
    """
    search_input = _find_search_input(page)
    if search_input is None:
        return False, "No search input found on page"

    # Clear existing text and type the search term
    try:
        search_input.click(timeout=3000)
        page.wait_for_timeout(300)
        # Triple-click to select all existing text, then type
        search_input.click(timeout=3000, click_count=3)
        page.wait_for_timeout(200)
        search_input.fill(term)
        page.wait_for_timeout(_SEARCH_TYPE_DELAY_MS)
    except Exception as exc:
        return False, f"Failed to type search term: {exc}"

    # Strategy 1: Press Enter
    try:
        search_input.press("Enter")
        page.wait_for_timeout(wait_ms)
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=10000)
        result_count = _read_result_count_text(page)
        if result_count:
            return True, f"Enter-key search triggered — result indicator: {result_count}"
        visible_cards = _count_job_cards(page)
        if visible_cards >= 1:
            return True, f"Enter-key search triggered — {visible_cards} visible cards"
    except Exception:
        pass

    # Strategy 2: Click search button
    search_btn = _find_search_button(page)
    if search_btn is not None:
        try:
            search_btn.click(timeout=3000)
            page.wait_for_timeout(wait_ms)
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=10000)
            result_count = _read_result_count_text(page)
            if result_count:
                return True, f"Button-click search triggered — result indicator: {result_count}"
            visible_cards = _count_job_cards(page)
            if visible_cards >= 1:
                return True, f"Button-click search triggered — {visible_cards} visible cards"
        except Exception:
            pass

    # Strategy 3: Assume real-time filtering (just typing is enough)
    page.wait_for_timeout(wait_ms)
    visible_cards = _count_job_cards(page)
    return True, f"Real-time filter assumed — {visible_cards} visible cards"


# ---------------------------------------------------------------------------
# Card click-through (interact mode)
# ---------------------------------------------------------------------------

_CARD_CLICK_SELECTORS = [
    # Moka-style: clickable job titles / cards
    "a.job-title", "a.position-name", ".job-card a", ".job-item a",
    "[class*='job-card'] a", "[class*='JobCard'] a", "[class*='position'] a",
    ".card a[href]", ".list-item a[href]",
    # Generic fallback: clickable elements inside visible card areas
    ".job-card", ".job-item", ".position-item", "[class*='job-card']", "[class*='JobCard']",
    "li a[href*='job']", "li a[href*='position']",
    # zhiye.com pattern
    "a[href*='jobDetail']", "a[href*='position_detail']",
    # Text-based fallback — buttons with "查看" or "详情"
    "button:has-text('查看')", "button:has-text('详情')", "a:has-text('查看')",
    # Category headers that expand to reveal cards
    "a:has-text('个职位')", "div:has-text('个职位')", "span:has-text('个职位')",
    "[class*='category'] a", "[class*='Category'] a", "[class*='tab'] a",
    ".recruit-list a", "[class*='recruit'] a",
]

# Category/section elements that need clicking to reveal job cards
_CATEGORY_EXPAND_SELECTORS = [
    "a:has-text('个职位')", "div:has-text('个职位')", "span:has-text('个职位')",
    "[class*='category']", "[class*='Category']", "[class*='tab']",
    "[class*='recruit-type']", "[class*='type-item']", "[class*='job-category']",
    "li:has-text('个职位')", ".position-category",
]

# Selectors for detail panel content after clicking
_DETAIL_CONTENT_SELECTORS = [
    ".job-detail", ".position-detail", ".job-desc", ".detail-content",
    "[class*='detail']", "[class*='Detail']", ".drawer-content",
    ".modal-body", ".popup-content", "[role='dialog']",
    ".job-info", ".position-info", ".jd-content",
]

# Buttons to close detail panels
_CLOSE_BUTTON_SELECTORS = [
    ".close", ".drawer-close", ".modal-close", "[aria-label='Close']",
    "[aria-label='close']", "button:has-text('×')", ".ant-drawer-close",
    ".el-drawer__close", ".moka-drawer-close",
]


def _expand_categories(page: Any, wait_ms: int) -> int:
    """Click category/section headers to reveal hidden job cards. Returns count clicked."""
    clicked = 0
    for sel in _CATEGORY_EXPAND_SELECTORS:
        try:
            elements = page.locator(sel).all()
            for el in elements:
                try:
                    if not el.is_visible():
                        continue
                    text = el.inner_text()
                    if not text.strip():
                        continue
                    el.click(timeout=3000)
                    page.wait_for_timeout(wait_ms)
                    clicked += 1
                except Exception:
                    continue
        except Exception:
            continue
    if clicked > 0:
        page.wait_for_timeout(wait_ms)
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=8000)
    return clicked


_FIND_CLICKABLE_CARDS_JS = """
() => {
    const results = [];
    const clickables = document.querySelectorAll('a, button, [role="button"]');
    const seen = new Set();

    const jobPatterns = [
        /届/, /校招/, /社招/, /实习/, /全职/, /提前批/, /内推/,
        /工程师/, /经理/, /专员/, /算法/, /开发/, /产品/, /设计/, /运营/,
        /Engineer/i, /Manager/i, /Developer/i, /Intern/i, /Scientist/i,
        /发布于/, /岗位/, /职位/,
    ];
    const skipPatterns = [
        /^\\s*$/, /^(首页|末页|登录|注册|搜索|筛选|清除|确定|取消|知道了|提交|保存)$/,
        /^(上一页|下一页|首页|末页|Home|Login|Search|Filter|Clear|Apply|Submit)$/,
        /^\\d+$/, /^(1|2|3|4|5)$/, /行\\/页/, /前往/,
        /^\\+\\d+$/,
    ];

    for (const el of clickables) {
        const text = (el.innerText || el.textContent || '').trim();
        if (!text || text.length < 3 || text.length > 200) continue;
        if (seen.has(text)) continue;

        const isJob = jobPatterns.some(p => p.test(text));
        const shouldSkip = skipPatterns.some(p => p.test(text));
        if (!isJob || shouldSkip) continue;

        let selector = '';
        if (el.id) selector = '#' + el.id;
        else if (el.className && typeof el.className === 'string') {
            const cls = el.className.trim().split(/\\s+/)[0];
            if (cls) selector = el.tagName.toLowerCase() + '.' + cls;
        }
        if (!selector) selector = el.tagName.toLowerCase();

        seen.add(text);
        results.push({tag: el.tagName.toLowerCase(), text: text, selector: selector});
        if (results.length >= 80) break;
    }
    return results;
}
"""


def _find_clickable_cards_js(page: Any, max_cards: int) -> list[dict[str, Any]]:
    """Find clickable job-card elements via JS. Returns [{tag, text, selector}]."""
    try:
        raw = page.evaluate(_FIND_CLICKABLE_CARDS_JS)
        return raw[:max_cards] if isinstance(raw, list) else []
    except Exception:
        return []


def _find_clickable_cards(page: Any, max_cards: int) -> list[Any]:
    """Find clickable job card elements on the page. Tries CSS selectors first,
    then falls back to JS-based element discovery."""
    candidates: list[Any] = []

    for sel in _CARD_CLICK_SELECTORS:
        try:
            elements = page.locator(sel).all()
            for el in elements:
                try:
                    if el.is_visible():
                        candidates.append(el)
                except Exception:
                    continue
            if len(candidates) >= max_cards:
                break
        except Exception:
            continue

    # If CSS selectors found nothing, try JS-based discovery and convert to locators
    if not candidates:
        js_cards = _find_clickable_cards_js(page, max_cards * 2)
        for card_info in js_cards:
            try:
                el = page.get_by_text(card_info["text"], exact=True).first
                if el.is_visible():
                    candidates.append(el)
                    continue
            except Exception:
                pass
            try:
                sel = card_info.get("selector", "")
                if sel:
                    el = page.locator(sel).first
                    if el.is_visible():
                        candidates.append(el)
            except Exception:
                continue
            if len(candidates) >= max_cards:
                break

    # Deduplicate by bounding box (roughly)
    unique: list[Any] = []
    seen_boxes: list[tuple[float, float, float, float]] = []
    for el in candidates:
        try:
            box = el.bounding_box()
            if box is None:
                unique.append(el)  # Can't get box, include anyway (JS-based elements)
                continue
            key = (round(box["x"], -1), round(box["y"], -1),
                   round(box["x"] + box["width"], -1), round(box["y"] + box["height"], -1))
            is_dup = False
            for sb in seen_boxes:
                if (abs(key[0] - sb[0]) < 30 and abs(key[1] - sb[1]) < 30 and
                        abs(key[2] - sb[2]) < 30 and abs(key[3] - sb[3]) < 30):
                    is_dup = True
                    break
            if not is_dup:
                seen_boxes.append(key)
                unique.append(el)
        except Exception:
            continue
        if len(unique) >= max_cards:
            break

    return unique[:max_cards]


def _extract_detail_text(page: Any) -> str:
    """Try to extract text from detail panel/drawer content."""
    for sel in _DETAIL_CONTENT_SELECTORS:
        try:
            panel = page.locator(sel).first
            if panel.is_visible():
                text = panel.inner_text()
                if len(text) > 50:  # meaningful content threshold
                    return text
        except Exception:
            continue
    # Fallback: body text (may include card list + detail)
    return _extract_body_text(page)


def _close_detail_panel(page: Any) -> None:
    """Try to close any open detail panel/drawer."""
    for sel in _CLOSE_BUTTON_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible():
                btn.click(timeout=3000)
                page.wait_for_timeout(1500)
                return
        except Exception:
            continue
    # Fallback: press Escape
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(1000)
    except Exception:
        pass


def _interact_on_cards(
    page: Any,
    current_url: str,
    recover_url: str,
    max_cards: int,
    wait_ms: int,
    label_prefix: str = "JOB",
) -> tuple[str, int, int, int]:
    """Click through job cards on an already-loaded page, collecting detail text.

    Returns (combined_text, clicked, found, failed).
    """
    cards = _find_clickable_cards(page, max_cards)
    if not cards:
        return ("", 0, 0, 0)

    detail_sections: list[str] = []
    clicked = 0
    failed = 0
    start_time = time.monotonic()
    time_budget = 120  # Max 2 minutes (bounded by the 150s interact deadline)

    for i, card in enumerate(cards):
        if time.monotonic() - start_time > time_budget:
            detail_sections.append(
                f"\n=== TIMEOUT: stopped after {clicked} cards ({failed} failed) ==="
            )
            break
        try:
            card.scroll_into_view_if_needed()
            page.wait_for_timeout(500)
            pre_text = _extract_body_text(page)

            card.click(timeout=3000)
            page.wait_for_timeout(min(wait_ms, 2000))
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=8000)

            post_text = _extract_body_text(page)

            if page.url != current_url:
                detail_text = _extract_detail_text(page)
                detail_sections.append(
                    f"\n=== {label_prefix} {i + 1} ({page.url}) ===\n{detail_text}"
                )
                page.go_back(timeout=10000)
                page.wait_for_timeout(wait_ms)
                with suppress(Exception):
                    page.wait_for_load_state("networkidle", timeout=10000)
                current_url = page.url
                clicked += 1
            elif len(post_text) > len(pre_text) + 50:
                detail_text = _extract_detail_text(page)
                detail_sections.append(
                    f"\n=== {label_prefix} {i + 1} ===\n{detail_text}"
                )
                _close_detail_panel(page)
                page.wait_for_timeout(1000)
                clicked += 1
            else:
                failed += 1

        except Exception:
            failed += 1
            try:
                if page.url != recover_url:
                    page.goto(recover_url, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(wait_ms)
                    current_url = page.url
            except Exception:
                pass
            continue

    return ("\n".join(detail_sections), clicked, len(cards), failed)


# ---------------------------------------------------------------------------
# Shared automation kernel
# ---------------------------------------------------------------------------


def _wait_stable_text(page: Any, *, min_chars: int = _MIN_USABLE_TEXT_CHARS) -> str:
    """Poll body text until it stops growing above the usable-text threshold.

    A below-threshold shell must NOT break early: it is exactly the pre-render
    state we are waiting out (SPA portals paint their job lists ~10s late).
    """
    page.wait_for_timeout(1_500)
    body_text = page.inner_text("body") or ""
    stable_samples = 0
    for _ in range(30):
        previous_len = len(body_text.strip())
        page.wait_for_timeout(500)
        body_text = page.inner_text("body") or ""
        current_len = len(body_text.strip())
        if current_len >= min_chars and current_len == previous_len:
            stable_samples += 1
            if stable_samples >= 2:
                break
        else:
            stable_samples = 0
    return body_text


def _browse_once(
    page: Any,
    target_url: str,
    *,
    mode: str = "render",
    pages: int = 3,
    max_cards: int = 5,
    wait_ms: int = 1500,
    term: str | None = None,
) -> dict[str, Any]:
    """Run one automation pass on a fresh page; returns the standard payload dict.

    mode:
      - "render":    default — render to stable text (the fetch fallback path)
      - "load-all":  render + aggressive scroll + load-more/show-all clicks
      - "paginate":  render page 1, then jump pages 2..N via the detected URL
                     page pattern (query / offset / path)
      - "interact":  render + click through up to ``max_cards`` job cards and
                     collect each detail (navigation / drawer / go_back)
      - "search":    render + type ``term`` into the in-site search box and
                     collect post-search state
    """
    response = page.goto(target_url, wait_until="domcontentloaded", timeout=20_000)
    if response is None:
        raise PublicFetchError("public_fetch_failed")
    status_code = response.status
    page.wait_for_timeout(1_500)
    for _ in range(3):
        _dismiss_consent(page)

    body = _wait_stable_text(page)
    title = page.title() or None
    links: list[str] = _collect_page_links(page, target_url)

    signals: dict[str, Any] = {
        "cards_visible": None,
        "estimated_total_items": None,
        "pagination_pattern": None,
        "strategy": None,
        "strategy_detail": None,
        "pages_collected": None,
        "warning": None,
        "search_ok": None,
        "search_detail": None,
        "pre_search_card_count": None,
        "post_search_card_count": None,
        "result_indicator": None,
    }

    if mode == "load-all":
        _scroll_to_load(page, wait_ms, rounds=2)
        got_more, rounds = _try_load_all_scroll(page, wait_ms, max_rounds=8)
        body = _wait_stable_text(page)
        signals.update(
            cards_visible=_count_job_cards(page),
            estimated_total_items=_estimate_total_items(page),
            strategy="load_all" if got_more else "render",
            strategy_detail=f"rounds={rounds}",
        )

    elif mode == "paginate":
        pattern = _detect_url_page_pattern(page.url or target_url)
        texts = [body]
        collected_links = list(links)
        if pattern is not None:
            for pg in range(2, pages + 1):
                jump_url = _build_page_url(page.url or target_url, pattern, pg)
                try:
                    page.goto(jump_url, wait_until="domcontentloaded", timeout=15_000)
                    page.wait_for_timeout(min(wait_ms, 1500))
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    break
                new_text = _wait_stable_text(page)
                if new_text.strip() == texts[-1].strip():
                    break  # invalid page → redirect/dup
                texts.append(new_text)
                for link in _collect_page_links(page, jump_url):
                    if link not in collected_links:
                        collected_links.append(link)
        body = "\n\n--- PAGE BREAK ---\n\n".join(texts)
        links = collected_links
        title = page.title() or title
        signals.update(
            pagination_pattern=pattern["type"] if pattern else None,
            pages_collected=len(texts),
            strategy="url_jump" if pattern else "single",
        )

    elif mode == "interact":
        _scroll_to_load(page, wait_ms, rounds=1)
        cats_clicked = _expand_categories(page, wait_ms)
        list_text = _extract_body_text(page)
        interact_text, clicked, found, failed = _interact_on_cards(
            page, page.url, target_url, max_cards, wait_ms, label_prefix="JOB"
        )
        if found > 0:
            body = f"=== LIST PAGE ===\n{list_text}\n{interact_text}"
        signals.update(
            cards_visible=_count_job_cards(page),
            estimated_total_items=_estimate_total_items(page),
            strategy="interact" if found > 0 else "render",
            strategy_detail=(
                f"categories_expanded={cats_clicked}, clicked={clicked}, "
                f"found={found}, failed={failed}"
            ),
        )
        if found == 0:
            signals["warning"] = (
                "No clickable job cards detected; returned list page text. "
                "Try mode=load-all or mode=paginate for this site."
            )

    elif mode == "search":
        pre_count = _count_job_cards(page)
        search_input = _find_search_input(page)
        if search_input is None:
            signals.update(
                search_ok=False,
                search_detail="No search input found on page",
                pre_search_card_count=pre_count,
            )
        else:
            ok, detail = _perform_search(page, term or "", wait_ms)
            post_count = _count_job_cards(page)
            indicator = _read_result_count_text(page)
            if post_count > 0 and post_count == pre_count and pre_count > 10:
                signals["warning"] = (
                    "Post-search card count equals pre-search count — search may "
                    "be a client-side fake filter; results may be incomplete."
                )
            signals.update(
                search_ok=ok,
                search_detail=detail,
                pre_search_card_count=pre_count,
                post_search_card_count=post_count,
                result_indicator=indicator,
            )
            body = _wait_stable_text(page)
        signals["cards_visible"] = _count_job_cards(page)

    else:  # "render" (default)
        signals.update(
            cards_visible=_count_job_cards(page),
            estimated_total_items=_estimate_total_items(page),
            strategy="render",
        )

    payload: dict[str, Any] = {
        "body": body,
        "title": title,
        "effective_url": page.url,
        "status_code": status_code,
        "links": links,
        **signals,
    }
    return payload


# ---------------------------------------------------------------------------
# Render cache (P1-2, module-level, bounded, TTL)
# ---------------------------------------------------------------------------


def _render_cache_key(
    url: str,
    collect_links: bool,
    mode: str,
    pages: int,
    max_cards: int,
    wait_ms: int,
    term: str | None,
) -> str:
    return "|".join(
        (
            mode,
            "links" if collect_links else "",
            str(pages),
            str(max_cards),
            str(wait_ms),
            term or "",
            canonical_request_url(url),
        )
    )


def _prune_render_cache() -> None:
    now = time.monotonic()
    expired = [k for k, v in _RENDER_CACHE.items() if v[0] <= now]
    for k in expired:
        _RENDER_CACHE.pop(k, None)
    if len(_RENDER_CACHE) > _RENDER_CACHE_MAX:
        for k in sorted(_RENDER_CACHE, key=lambda k: _RENDER_CACHE[k][0])[
            : len(_RENDER_CACHE) - _RENDER_CACHE_MAX
        ]:
            _RENDER_CACHE.pop(k, None)


def clear_render_cache() -> None:
    """Reset the module-level render cache (test seam)."""
    _RENDER_CACHE.clear()


# ---------------------------------------------------------------------------
# Payload / signals accessors (tuple-API compatibility)
# ---------------------------------------------------------------------------


def _set_render_payload(payload: dict[str, Any]) -> None:
    _RENDER_PAYLOAD.value = payload


def _render_payload() -> dict[str, Any] | None:
    return getattr(_RENDER_PAYLOAD, "value", None)


def _render_signals() -> dict[str, Any]:
    """The steering signals of the most recent browser op (or empty dict)."""
    payload = _render_payload()
    if not payload:
        return {}
    return {
        key: payload.get(key)
        for key in (
            "cards_visible",
            "estimated_total_items",
            "pagination_pattern",
            "strategy",
            "strategy_detail",
            "pages_collected",
            "warning",
            "search_ok",
            "search_detail",
            "pre_search_card_count",
            "post_search_card_count",
            "result_indicator",
        )
    }


def _render_metadata(url: str) -> tuple[str, int | None]:
    """Read the most recent render's final URL/status without changing tuple APIs."""
    metadata = getattr(_RENDER_METADATA, "value", None)
    if metadata is None:
        return url, 200
    return metadata


def _set_render_metadata(url: str, status_code: int | None) -> None:
    _RENDER_METADATA.value = (url, status_code)


def _mode_timeout_s(mode: str) -> int:
    return _MODE_TIMEOUT_S.get(mode, _RENDER_TIMEOUT_S)


# ---------------------------------------------------------------------------
# Worker command / process
# ---------------------------------------------------------------------------


def _playwright_worker_command(
    url: str,
    *,
    collect_links: bool = False,
    mode: str = "render",
    pages: int = 3,
    max_cards: int = 5,
    wait_ms: int = 1500,
    term: str | None = None,
) -> list[str]:
    """Build the isolated render-worker command without shell interpolation."""
    command = [
        sys.executable,
        "-m",
        "pi_career_skills.network.playwright_worker",
        "--url",
        url,
    ]
    if collect_links:
        command.append("--collect-links")
    if mode != "render":
        command += ["--mode", mode]
    if pages != 3:
        command += ["--pages", str(pages)]
    if max_cards != 5:
        command += ["--max-cards", str(max_cards)]
    if wait_ms != 1500:
        command += ["--wait-ms", str(wait_ms)]
    if term:
        command += ["--term", term]
    return command


def _render_with_playwright_process(
    url: str,
    *,
    collect_links: bool = False,
    mode: str = "render",
    pages: int = 3,
    max_cards: int = 5,
    wait_ms: int = 1500,
    term: str | None = None,
) -> tuple[str, str | None] | tuple[str, str | None, list[str]]:
    """Render in a killable child process so Chromium cannot become an orphan."""
    _assert_public_url(url)
    kwargs: dict[str, Any] = {
        "cwd": str(Path(__file__).resolve().parents[3]),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(
        _playwright_worker_command(
            url,
            collect_links=collect_links,
            mode=mode,
            pages=pages,
            max_cards=max_cards,
            wait_ms=wait_ms,
            term=term,
        ),
        **kwargs,
    )
    try:
        stdout, _stderr = process.communicate(timeout=_mode_timeout_s(mode))
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process.pid)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise PublicFetchError("public_fetch_failed") from None
    if process.returncode != 0:
        raise PublicFetchError("public_fetch_failed") from None
    try:
        payload = json.loads((stdout or "").strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        raise PublicFetchError("public_fetch_failed") from None
    if payload.get("error"):
        raise PublicFetchError(
            str(payload["error"]),
            effective_url=payload.get("effective_url")
            if isinstance(payload.get("effective_url"), str)
            else url,
            status_code=payload.get("status_code")
            if isinstance(payload.get("status_code"), int)
            else None,
        )
    body = payload.get("body")
    title = payload.get("title")
    effective_url = payload.get("effective_url")
    status_code = payload.get("status_code")
    _set_render_metadata(
        effective_url if isinstance(effective_url, str) else url,
        status_code if isinstance(status_code, int) else 200,
    )
    _set_render_payload(payload)
    if not isinstance(body, str):
        raise PublicFetchError("public_fetch_failed")
    if collect_links:
        links = payload.get("links")
        return body, title if isinstance(title, str) else None, (
            links if isinstance(links, list) else []
        )
    return body, title if isinstance(title, str) else None


def enable_playwright_fallback(enabled: bool) -> None:
    """Toggle the rendered-fetch fallback (called from runtime assembly)."""
    global _PLAYWRIGHT_FALLBACK_ENABLED
    _PLAYWRIGHT_FALLBACK_ENABLED = enabled


def configure_playwright_storage_state(path: str | None) -> None:
    """Ignore profile paths: Phase 6 is a public-web-only renderer."""
    del path
    global _PLAYWRIGHT_STORAGE_STATE_PATH
    _PLAYWRIGHT_STORAGE_STATE_PATH = None


def _run_browser_op(
    url: str,
    *,
    collect_links: bool = False,
    mode: str = "render",
    pages: int = 3,
    max_cards: int = 5,
    wait_ms: int = 1500,
    term: str | None = None,
) -> dict[str, Any]:
    """Dispatch one browser operation and return the full payload dict.

    Three execution paths:
      1. seam ``_PLAYWRIGHT_FETCH_IMPL`` (unit tests) — no browser, synthetic payload;
      2. real playwright — one-shot killable child process under ``_RENDER_LOCK``,
         served from / stored into the module-level bounded TTL cache;
      3. injected fake sync API — in-process render under a watchdog, with the
         relaunch-once policy (RC-A).
    """
    if _PLAYWRIGHT_FETCH_IMPL is not None:
        rendered = _PLAYWRIGHT_FETCH_IMPL(url)
        _set_render_metadata(url, 200)
        body, title = rendered[:2]
        return {
            "body": body,
            "title": title,
            "effective_url": url,
            "status_code": 200,
            "links": list(rendered[2]) if len(rendered) >= 3 else [],
            "cards_visible": None,
            "estimated_total_items": None,
            "pagination_pattern": None,
            "strategy": None,
            "strategy_detail": None,
            "pages_collected": None,
            "warning": None,
            "search_ok": None,
            "search_detail": None,
            "pre_search_card_count": None,
            "post_search_card_count": None,
            "result_indicator": None,
        }
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise PublicFetchError("public_fetch_failed") from None

    # Real Playwright runs in an isolated one-shot process. Unit tests can
    # still inject the sync API seam; injected fakes remain in-process so the
    # existing deterministic browser contract tests do not need a real browser.
    if getattr(sync_playwright, "__module__", "").startswith("playwright"):
        with _RENDER_LOCK:
            key = _render_cache_key(
                url, collect_links, mode, pages, max_cards, wait_ms, term
            )
            cached = _RENDER_CACHE.get(key)
            if cached is not None:
                expires, payload = cached
                if expires > time.monotonic():
                    _set_render_metadata(
                        payload.get("effective_url")
                        if isinstance(payload.get("effective_url"), str)
                        else url,
                        payload.get("status_code")
                        if isinstance(payload.get("status_code"), int)
                        else 200,
                    )
                    return payload
                _RENDER_CACHE.pop(key, None)
            _render_with_playwright_process(
                url,
                collect_links=collect_links,
                mode=mode,
                pages=pages,
                max_cards=max_cards,
                wait_ms=wait_ms,
                term=term,
            )
            payload = _render_payload() or {}
            if not payload.get("body"):
                raise PublicFetchError("public_fetch_failed")
            _prune_render_cache()
            _RENDER_CACHE[key] = (time.monotonic() + _RENDER_CACHE_TTL_S, payload)
            return payload

    # Injected fake sync API: render in-process, guarded by a hard watchdog.
    global _PLAYWRIGHT_RUNTIME

    def _render_once(browser: Any, target_url: str) -> dict[str, Any]:
        """One automation pass against ``browser``; the caller owns retry policy."""
        page = browser.new_page()

        def _abort_non_public(route: Any, request: Any) -> None:
            try:
                if _is_public_url(request.url):
                    route.continue_()
                else:
                    route.abort()
            except Exception:
                route.abort()

        page.route("**/*", _abort_non_public)
        try:
            return _browse_once(
                page,
                target_url,
                mode=mode,
                pages=pages,
                max_cards=max_cards,
                wait_ms=wait_ms,
                term=term,
            )
        finally:
            page.close()

    def _run_render() -> dict[str, Any]:
        """Launch-or-reuse the shared browser and render; relaunch once."""
        global _PLAYWRIGHT_RUNTIME
        attempt = 0
        while True:
            pw, browser = _PLAYWRIGHT_RUNTIME or (None, None)
            if browser is None:
                try:
                    pw = sync_playwright().start()
                    browser = pw.chromium.launch(headless=True)
                    _PLAYWRIGHT_RUNTIME = (pw, browser)
                except Exception:
                    if pw is not None:
                        with suppress(Exception):
                            pw.stop()
                    raise PublicFetchError("public_fetch_failed") from None
            try:
                return _render_once(browser, url)
            except PublicFetchError:
                raise
            except Exception:
                # The shared browser died mid-render (crash / OOM / CDP
                # disconnect). Every later attempt against the dead runtime
                # would fail in ~0.0s, so tear it down and relaunch exactly
                # once (RC-A). A PublicFetchError is never retried:
                # security/validation rejections and the deliberate
                # blocked-page path stay final.
                if attempt >= 1:
                    raise PublicFetchError("public_fetch_failed") from None
                attempt += 1
                with suppress(Exception):
                    browser.close()
                with suppress(Exception):
                    pw.stop()
                _PLAYWRIGHT_RUNTIME = None

    # Serialize renders (playwright sync is thread-affine; the C5 batch fetch
    # runs 4 threads) and bound each call with a hard watchdog. A wedged
    # driver must not hang the whole eval process: the worker is a daemon,
    # the runtime is orphaned for the next call to replace, and teardown is
    # never attempted from a foreign thread (that call is itself the hang).
    global _PLAYWRIGHT_RUNTIME
    with _RENDER_LOCK:
        boxed: list[dict[str, Any]] = []
        errors: list[BaseException] = []

        def _watchdog_target() -> None:
            try:
                boxed.append(_run_render())
            except BaseException as exc:  # re-raised on caller
                errors.append(exc)

        worker = threading.Thread(target=_watchdog_target, daemon=True)
        worker.start()
        worker.join(timeout=_mode_timeout_s(mode))
        if worker.is_alive():
            _PLAYWRIGHT_RUNTIME = None
            raise PublicFetchError("public_fetch_failed")
        if errors:
            raise errors[0]
        return boxed[0]


def _render_with_playwright(
    url: str,
    *,
    collect_links: bool = False,
    mode: str = "render",
    pages: int = 3,
    max_cards: int = 5,
    wait_ms: int = 1500,
    term: str | None = None,
) -> tuple[str, str | None] | tuple[str, str | None, list[str]]:
    """Render ``url`` in headless Chromium; return (body_text, title[, links]).

    Uses the seam ``_PLAYWRIGHT_FETCH_IMPL`` when injected (unit tests); the
    real path lazily imports playwright and runs one-shot child processes.
    A per-request route guard aborts any request whose destination is not a
    global public address, mirroring ``_assert_public_url`` inside the
    rendered page (SPA redirects and fetch() subresources included).
    With ``collect_links=True`` the rendered DOM's same-host job-shaped
    ``<a href>`` targets are also returned (list-page expansion, P2).

    ``mode`` / ``pages`` / ``max_cards`` / ``wait_ms`` / ``term`` select the
    automation mode (see ``_browse_once``); the tuple contract is unchanged.
    The richer payload — steering signals included — is always available via
    ``_render_payload()`` / ``_render_signals()`` after the call.

    The real path relaunches the shared browser exactly once (RC-A): a
    generic crash / OOM / CDP-disconnect mid-render tears the dead runtime
    down and retries, so one dead browser can never fail every later render
    in the process.  A raised ``PublicFetchError`` is never retried --
    security/validation rejections stay final.
    """
    payload = _run_browser_op(
        url,
        collect_links=collect_links,
        mode=mode,
        pages=pages,
        max_cards=max_cards,
        wait_ms=wait_ms,
        term=term,
    )
    _set_render_payload(payload)
    body = payload.get("body")
    title = payload.get("title")
    if not isinstance(body, str):
        raise PublicFetchError("public_fetch_failed")
    if collect_links:
        links = payload.get("links")
        return body, title, links if isinstance(links, list) else []
    return body, title


def _browse_with_playwright(
    url: str,
    *,
    mode: str = "render",
    pages: int = 3,
    max_cards: int = 5,
    wait_ms: int = 1500,
    term: str | None = None,
) -> dict[str, Any]:
    """Run one browser automation op (render / load-all / paginate / interact /
    search) and return the full payload: body, title, effective_url,
    status_code, links, and the steering signals."""
    payload = _run_browser_op(
        url,
        collect_links=True,
        mode=mode,
        pages=pages,
        max_cards=max_cards,
        wait_ms=wait_ms,
        term=term,
    )
    _set_render_payload(payload)
    if not payload.get("body"):
        raise PublicFetchError("public_fetch_failed")
    return payload


def _render(
    url: str,
    *,
    collect_links: bool = False,
    mode: str = "render",
    pages: int = 3,
    max_cards: int = 5,
    wait_ms: int = 1500,
    term: str | None = None,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            browser_context = browser.new_context()
            page = browser_context.new_page()

            def abort_non_public(route: Any, request: Any) -> None:
                try:
                    if _is_public_url(request.url):
                        route.continue_()
                    else:
                        route.abort()
                except Exception:
                    route.abort()

            page.route("**/*", abort_non_public)
            try:
                payload = _browse_once(
                    page,
                    url,
                    mode=mode,
                    pages=pages,
                    max_cards=max_cards,
                    wait_ms=wait_ms,
                    term=term,
                )
                if not collect_links:
                    payload.pop("links", None)
                return payload
            finally:
                page.close()
                browser_context.close()
        finally:
            browser.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--collect-links", action="store_true")
    parser.add_argument(
        "--mode",
        choices=["render", "load-all", "paginate", "interact", "search"],
        default="render",
    )
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--max-cards", type=int, default=5)
    parser.add_argument("--wait-ms", type=int, default=1500)
    parser.add_argument("--term", default=None)
    args = parser.parse_args()
    try:
        payload = _render(
            args.url,
            collect_links=args.collect_links,
            mode=args.mode,
            pages=args.pages,
            max_cards=args.max_cards,
            wait_ms=args.wait_ms,
            term=args.term,
        )
    except PublicFetchError as exc:
        payload = {"error": exc.code}
    except Exception:
        payload = {"error": "public_fetch_failed"}
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()


__all__ = [
    "_PLAYWRIGHT_FALLBACK_CODES",
    "_PLAYWRIGHT_FALLBACK_ENABLED",
    "_PLAYWRIGHT_STORAGE_STATE_PATH",
    "_PLAYWRIGHT_FETCH_IMPL",
    "_PLAYWRIGHT_RUNTIME",
    "_render_metadata",
    "_set_render_metadata",
    "enable_playwright_fallback",
    "configure_playwright_storage_state",
    "_render_with_playwright",
    "_browse_with_playwright",
    "_render_signals",
    "_render_payload",
    "clear_render_cache",
]
