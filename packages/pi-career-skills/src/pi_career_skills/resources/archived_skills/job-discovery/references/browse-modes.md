# browse.py Modes Reference

Load this document on demand when you need the **full reference for `scripts/browse.py`
modes** (what each mode does, when to use which, search-strategy options, output format).
For the single-URL extraction *workflow* (planner -> executor -> verifier), see
`single-url-extraction.md` instead.

Run `scripts/browse.py` with the appropriate mode:

```bash
python scripts/browse.py "<url>" \
  --mode list|detail|interact|search|search-interact|parallel-fetch|click \
  --out output/evidence \
  --max-pages 5 \
  --wait 3000
```

**Modes:**
- `list` - For listing/search pages where JDs are visible inline. Waits for render,
  scrolls to load lazy content, detects pagination, and collects text from all visible
  content across up to `--max-pages` pages. **Best for**: static sites, simple career pages.
- `detail` - For single job detail pages. Opens the URL, waits for render, returns
  the full `body.innerText`.
- `interact` - For sites where JDs are hidden behind click interactions (Moka, some SPAs).
  Expands category/section headers, then uses JS-based element discovery to find and
  click job cards one by one. Captures expanded text from each. **Note**: This mode
  has a 2-minute time budget and works best when cards reveal content inline (rather
  than in pure-SPA drawers).
- `search` - **Search-first mode**. Finds a search box on the page, enters keywords,
  then browses only the filtered results (with pagination). Falls back to full `list`
  mode if search is unavailable or produces zero results (with `--fallback full`).
  **Best for**: high-page-count career sites (Moka, zhiye.com, Feishu) where you want
  to narrow down results before extracting.
- `search-interact` - **Optimal mode for Moka/zhiye.com/Feishu**. Combines `search` +
  `interact`: first filters by keyword, then clicks through each filtered card to
  capture expanded full JDs. Falls back to `search` mode if no clickable cards are
  found, and to `list` mode if search itself is unavailable. This is the recommended
  mode for most career platforms when `parallel-fetch` is not applicable.
- `parallel-fetch` - **v1.6 default for URL-keyed paginated sites** (e.g. xiaomi
  Mioffice, bytedance jobs.bytedance.com, mokahr). Detects URL-keyed pagination
  (click next -> read URL -> click prev -> read URL -> diff query params to find the
  page-number and page-size parameters), pre-computes ALL page URLs, then fetches
  pages concurrently via a thread pool (Java-ThreadPoolExecutor analog) with a
  per-worker persistent browser. **Auto-fallback**: `click` mode for load-more/opaque
  pagination, `spa_shell_no_pagination` thin result for card-SPAs, `status=blocked`
  for anti-bot walls. **Best for**: multi-page URL-keyed listings where you want to
  cut the browse leg from ~300s serial to ~70s parallel. When `used_path` is not
  `parallel`, retry once with `--mode search-interact`.
- `click` - Agent-driven sequential click pagination (click next -> collect -> repeat).
  Used as the `parallel-fetch` fallback for load-more sites.

**Search mode keywords:**

```bash
# Default keywords (broad coverage):
python scripts/browse.py "<url>" --mode search-interact

# Custom keywords for specific roles:
python scripts/browse.py "<url>" --mode search-interact \
  --search-terms "AI,Agent,大模型,LLM,人工智能,深度学习"

# Strategy: first_match (stop at first keyword with results - fastest)
python scripts/browse.py "<url>" --mode search-interact \
  --search-strategy first_match

# Strategy: each (try all keywords, merge & deduplicate - most thorough)
python scripts/browse.py "<url>" --mode search-interact \
  --search-strategy each

# Strategy: broad (use only the first keyword - e.g. a wide term like "AI")
python scripts/browse.py "<url>" --mode search-interact \
  --search-strategy broad \
  --search-terms "AI"

# No fallback - fail explicitly if search unavailable:
python scripts/browse.py "<url>" --mode search --fallback none
```

**When to use which mode:**

| Scenario | Mode | Why |
|----------|------|-----|
| URL-keyed multi-page listing (xiaomi/bytedance/mokahr) | `parallel-fetch` | Pre-compute page URLs, fetch concurrently -> ~70s instead of ~300s serial |
| Moka, 50+ page listings | `search-interact` | Search -> filter to 1-2 pages -> click each card -> full JDs |
| Moka, small site (< 10 pages) | `interact` | Skip search overhead, just click-through |
| zhiye.com, many pages | `search-interact` | Search box usually available |
| Feishu, many pages | `search-interact` | Search box sometimes available; auto-fallback if not |
| WeChat article | `detail` -> OCR pipeline | No search box; static content |
| Custom SPA without search | `interact` or `list` | No search box available |
| Single detail page | `detail` | One page, no search needed |

**What it does:**
1. Launches headless Chromium
2. Navigates to the URL, waits for `networkidle`
3. Dismisses common consent/GDPR dialogs automatically
4. Scrolls to trigger lazy-loaded content
5. In `list` mode: finds "next page" buttons and paginates (up to `--max-pages`)
6. Saves: `output/evidence/<content_hash>.txt` (full page text) and `.png` (screenshot)
7. Outputs a JSON result to stdout with status, text preview, and content hash

**Output format (stdout):**
```json
{
  "status": "ok",
  "url": "https://...",
  "title": "Page title",
  "content_hash": "sha256_abc123...",
  "text_path": "output/evidence/sha256_abc123.txt",
  "screenshot_path": "output/evidence/sha256_abc123.png",
  "job_count_estimate": 42,
  "pagination": {"current": 1, "total": 3, "has_more": true}
}
```

If `status` is `"blocked"` or `"error"`, skip to the next URL and record the reason.

**Content-addressed caching:** The text file path is derived from `sha256(page_text)`.
If the file already exists, `browse.py` skips the browser and returns the cached path
immediately. This means re-running on the same URL costs nothing.
