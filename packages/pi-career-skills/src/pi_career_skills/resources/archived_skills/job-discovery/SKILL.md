---
name: job-discovery
description: >
  Automated job posting discovery from Tencent Smartsheet career URLs. Reads source URLs from
  smartsheet, browses career sites via Playwright to capture page text, then uses LLM extraction
  to produce structured job descriptions. Use when the user wants to discover, collect, or extract
  job postings from career sites, crawl recruitment pages, sync job data from Tencent Docs, or
  batch-extract position details from any list of career URLs. Also use when the user mentions
  "抓取招聘信息", "提取岗位JD", "批量爬职位", "同步招聘数据", or similar Chinese phrases.
compatibility: requires Python 3.10+, playwright (pip), tencent-docs skill (for smartsheet access)
---

# Job Discovery Agent

Extract structured job descriptions from career site URLs at scale. Designed as a
pi-agent skill - the LLM (you) orchestrates, helper scripts handle the mechanical work
(browser rendering, caching, validation).

**This file is a dispatch hub.** It is intentionally short. Load the reference file
that matches your task from the [Progressive disclosure](#progressive-disclosure-how-deep-to-go)
table or the [References](#references) list - do NOT read them all up front.

## Why this skill exists

Career sites come in dozens of shapes - Moka, Feishu, zhiye.com, custom React SPAs,
WeChat articles. Writing deterministic scrapers for each is brittle and high-maintenance.
Instead, this skill uses:

1. **Playwright** to render JS-heavy pages into plain, readable text (once per URL)
2. **Your LLM reasoning** to classify sites and extract structured JDs from that text
3. **Content-addressed caching** so no page is rendered or extracted twice

The result is a pipeline that adapts to new site types without new code - only new
instructions in `references/site-catalog.md`.

## Adapter-aware execution

Use `scripts/adapter_supervisor.py` as the outer Supervisor when a backend
`DomainAdapter` is supplied. Build the planner with `build_skill_deep_agent()`
(`deepagents.create_deep_agent`) and give its tools the normal Skill operations:
`browse`, `read_evidence`, `write_candidates`, `validate`, and `deduplicate`.

- No adapter: run the normal Skill workflow.
- Adapter success: retain its result and trajectory as evidence.
- Adapter failure at any point: run the normal Skill workflow with the complete
  trajectory context; never return a partial adapter result as success.

Before reporting a complete run, call `scripts/coverage_gate.py` on the merged
candidates and page files. Only call a run coverage-verified when it has page
evidence, a positive terminal signal, a body for every candidate, no duplicate
public apply URL identity (or, when URLs are unavailable, duplicate
title/department/location identity), and (when public total is known) an exact
count.

The fallback still uses `parallel-fetch` first, makes only one
`search-interact` retry for a thin SPA, fans out one extraction sub-agent per
page file, then runs deterministic validation and deduplication.  Treat a
login/captcha/anti-bot result as manual review, never as a retry target.

## Certified public-JSON adapters (A1, backend-gated)

Three sites that browse.py classifies as anti-bot-blocked (didi, netease,
baidu) expose official, unauthenticated, public JSON listing endpoints.  The
`scripts/adapters/` package (one `Adapter` module per company, contract =
`validate(url)` + `execute(task, strategy, trajectory)` like
`adapter_supervisor.py`) is a legal public data channel: no bypass, TLS
verification on, polite 0.2-0.5s pacing, 300 items/company hard cap.  It is a
**fetch-only channel** - the collector stays passive; adapter output becomes
ordinary page evidence (source_url + content_hash).

- Every endpoint and apply/detail URL passes `is_safe_public_url` before use.
- The whole channel is gated twice: `endpoint_allowlist.json` must carry
  `review_status: "reviewed"` (human-reviewed; see the file's `reviewed_by` /
  `reviewed_on`), and the backend flag `use_public_api_adapters` must be on
  (default off).  Adapter hosts are fetched adapter-first only when the flag
  is on; otherwise the channel never runs.
- Adapter failure is an explicit `blocked` terminal (`adapter:<code>`), never
  a browse fallback and never a silent empty result - same semantics as the
  anti-bot block.  Codes: `url_not_allowlisted`, `empty_result`,
  `malformed_payload`, `adapter_error`, `adapter_unknown`, `adapter_invalid`,
  `allowlist_*`, `http_error:*`, `timeout`, `dns_error`, `transport_error`.
- Human smoke run: `python -m adapters <company> <url>` from `scripts/`
  (prints the JSON records; `blocked: <code>` on failure).
- Run `python -m adapters` with no args for usage.  Never modify the
  allowlist without recording a human review.

## Quick start

```bash
# 1. Ensure dependencies
pip install playwright && playwright install chromium

# 2. Single URL (L2) - the common case. Follow references/single-url-extraction.md:
#    Planner -> Executor -> Verifier. Browse with parallel-fetch first.
python scripts/browse.py "https://xiaopeng.jobs.feishu.cn/s/Pycfxid-fok" \
  --mode parallel-fetch --max-pages 20 --out output/evidence

# 3. Read the workflow doc, then extract + validate (you - the LLM - do this step)
#    see references/single-url-extraction.md and references/schema.md

# 4. Validate the extracted candidates
python scripts/validate.py output/candidates/<hash>.json

# 5. Batch (L3) - read URLs from Tencent Smartsheet, dedup across runs:
#    see references/smartsheet-sources.md and references/incremental-persistence.md
```

## Full workflow

There are six phases. The single-URL path (L2) covers Phases 2-4 with `browse.py` +
LLM extract + `validate.py`; the batch path (L3) adds Phases 1, 5, 6 with `state.json`
incremental logic.

### Phase 1 - INGEST: Collect URLs (L3 only)

Read career URLs from the Tencent Smartsheets. Sheet IDs, field mappings, and the
ingest commands live in **`references/smartsheet-sources.md`**.

### Phase 2 - CLASSIFY: Determine site type and extraction strategy

For each URL, do a lightweight probe before committing to a full browser render:

```bash
# Fetch just the first 4KB of HTML
curl -sL --max-time 10 "<url>" | head -c 4096 > /tmp/preview.txt
```

Read `/tmp/preview.txt` and classify:

| Signal | Likely site type | Recommended approach |
|--------|-----------------|---------------------|
| `mp.weixin.qq.com` in URL | WeChat article | `browse.py --mode detail` -> check text_length -> if image-heavy, OCR -> channel triage -> recursive browse (see `wechat-image-handling.md`, 6-level pipeline) |
| Multi-page listing (mokahr / bytedance / Mioffice / any paginated) | URL-keyed SPA | `browse.py --mode parallel-fetch` (v1.6 default; auto-falls back to `click` for load-more sites) |
| `jobs.feishu.cn` in URL | Feishu/Lark | `parallel-fetch`, retry `search-interact` if thin |
| `zhiye.com` in URL | zhiye.com platform | `browse.py --mode search-interact` (search box usually available) |
| `<script>` with `__NEXT_DATA__` | Next.js/Nuxt SPA | `browse.py --mode search` or `list` |
| Login wall / 403 / captcha | Blocked | Skip, mark as `needs_manual_review` |
| Plain HTML with listings in first 4KB | Static site | `curl` full page OR `browse.py --mode list` |

Record your classification decision and proceed to Phase 3. **Why classify first?**
`browse.py` takes 15-30 seconds per URL; skipping blocked URLs and routing WeChat
through ReadGZH saves significant time at scale.

### Phase 3 - EXTRACT: Render page text

Run `scripts/browse.py` with the appropriate mode. The full mode reference (what each
mode does, search-strategy options, output format) lives in **`references/browse-modes.md`**.
For the single-URL *workflow* (planner -> executor -> verifier), read
**`references/single-url-extraction.md`**.

Condensed mode list:

| Mode | One-liner |
|------|-----------|
| `parallel-fetch` | v1.6 default for URL-keyed paginated sites; pre-computes page URLs, fetches concurrently via thread pool; auto-falls back to `click` |
| `search-interact` | Moka/zhiye/Feishu: search-filter then click each card for full JDs |
| `list` / `detail` / `interact` / `search` / `click` | See `references/browse-modes.md` |

### Phase 4 - STRUCTURE: LLM extracts normalized JDs

This is your core contribution as the LLM orchestrator. Read the page text and
extract every job posting into the `NormalizedJobCandidate` schema.

1. Read `output/evidence/<content_hash>.txt`.
2. Consult **`references/extraction-guide.md`** for site-specific tips and
   **`references/schema.md`** for the full schema.
3. Extract all positions into a JSON array; save to `output/candidates/<hash>.json`.
4. Validate: `python scripts/validate.py output/candidates/<hash>.json --package --verify`.

**`confidence` calibration by `evidence_type`** (this is what schema.md Phase 4 refers to):
`browsed_detail_page` -> 0.88-0.95; `ocr_full_jd_text` -> 0.60-0.75;
`ocr_poster_keyword` -> 0.40-0.55; a poster with only "AI应用" and no JD body stays
at 0.45 max regardless of OCR accuracy.

### Phase 5 - NORMALIZE & DEDUPLICATE (L3)

**L1 (lightweight):** `python scripts/normalize.py --title "..."` for a single title
or `core_hash`. **L3 (full batch):**
`python scripts/deduplicate.py output/candidates/*.json --out output/merged_final.json`
(normalize, semantic-dedup, idempotency keys, quality checks, merge). Full details in
**`references/incremental-persistence.md`**.

### Phase 6 - PERSIST: Collect and report (L3)

Review `merged_final.json` and update `state.json`. Full details in
**`references/incremental-persistence.md`**.

## Error handling guide

| Situation | Action |
|-----------|--------|
| URL returns 403 / login wall | Skip, record in `output/errors.jsonl` |
| Page renders but has no job listings | Mark as `empty`, record screenshot path |
| Page has >100 positions (estimate) | Process every discovered page concurrently in bounded batches; checkpoint each page result. A configured `--max-pages` ceiling is a safety stop, not proof of completion: report `needs_manual_review` unless a positive terminal marker proves the final page was reached. |
| LLM extraction produces invalid JSON | Re-read the text and try again with stricter prompt |
| Playwright times out (30s+) | Retry once with `--wait 5000`, then skip |
| WeChat article has images (any) | ALWAYS attempt OCR - see `references/wechat-image-handling.md` for the full decision tree (6 levels) |
| WeChat article: OCR done but only keywords, no JD body | Classify channel -> if URL found, recursively browse career site (Level 6) |
| Recursive browse of career URL returns only navigation (SPA) | Mark `needs_deep_crawl`, save OCR JDs, append to errors.jsonl (Level 6 Step 4) |
| Recursive browse succeeds with full JDs | Replace OCR extraction with browsed JDs, confidence 0.85+ (Level 6 Step 2B) |
| Search mode: no search box found | Auto-fallback to `list` mode (with `--fallback full`) |
| Search mode: keyword returns 0 results | Try next keyword (first_match) or fallback to full list |
| Search mode: post-search count == pre-count | Warning logged - possible client-side fake filter; results may be incomplete |
| Login wall on a registered site (L2ac) | Check with `scripts/check_login.py`; run `scripts/login.py` for the human login; then re-crawl with `scripts/crawl.py` |
| 403 / 429 / slider / captcha (L2ac) | Anti-crawl layer: exponential backoff (30/60/120s x3) or human-assisted challenge; surface as `blocked:<code>` |
| Rate-limited after 3 backoffs (L2ac) | Report `blocked:rate_limited`, stop crawling that site for the day |

## Login & anti-crawl (L2ac)

For target sites that hit the anti-crawl registry (`anti_crawl/site_registry.py`) or show a
login wall / 403 / slider / captcha: use this layer instead of plain `browse.py`. Only
registered sites, personal accounts, personal job-seeking use.

```bash
python scripts/check_login.py [--site liepin]   # health check: login + anti-crawl status
python scripts/login.py --site liepin           # interactive one-time login (QR/SMS/slider), persistent per-site profile
python scripts/crawl.py --site liepin --keyword "AI" --max-pages 3   # logged-in crawl
```

- `crawl.py` output is isomorphic with `browse.py` (same `output/evidence/` dir, same sha256
  evidence naming) - Phase 4-6 unchanged.
- Discipline: polite pacing by default (random 2-5s/page, single-page concurrency, daily cap 500
  pages); 403/429 exponential backoff; sliders/captchas are **human-assisted only** (headed
  window, 5s poll, 5min timeout) - never automated bypass.
- Signature-parameter sites are out of scope here (Firefox-Reverse toolchain territory); label
  them honestly. See `references/anti-crawl-guide.md` + `references/site-adapters.md`.
## References

Load these as needed during processing:

- `references/single-url-extraction.md` - **L2 workflow**: Planner -> Executor -> Verifier for one career URL (parallel-fetch first)
- `references/browse-modes.md` - Full `browse.py` mode reference (list/detail/interact/search/search-interact/parallel-fetch/click)
- `references/site-catalog.md` - Known career site patterns, selectors, and quirks
- `references/extraction-guide.md` - Detailed JD extraction rules with examples
- `references/schema.md` - Full NormalizedJobCandidate JSON schema
- `references/wechat-image-handling.md` - WeChat article full pipeline: OCR strategy (5 levels) + channel triage & recursive browsing (Level 6)
- `references/smartsheet-sources.md` - **L3**: Sheet A/B IDs, field mappings, Phase 1 INGEST
- `references/incremental-persistence.md` - **L3**: state.json, three-tier change detection, Phase 5/6 dedup/persist, resumability
- `references/anti-crawl-guide.md` - **L2ac**: login + anti-crawl decision tree (check_login -> login -> crawl); status semantics; compliance red lines
- `references/site-adapters.md` - **L2ac**: anti-crawl site profiles (moka/nowcoder/baidu/58/liepin): defense types, login signals, search URL templates

## Progressive disclosure: how deep to go

This skill is designed with usage levels. Start shallow; go deeper only when needed.

| Level | What you load | When to use |
|-------|--------------|-------------|
| **L1: Quick normalize** | `scripts/normalize.py --title "..."` | Comparing two job titles, computing a `core_hash` for identity |
| **L2: Single URL** | `SKILL.md` + `references/single-url-extraction.md` (+ `browse-modes.md`, `schema.md`, `extraction-guide.md` as needed) | Processing one career page end-to-end |
| **L2w: WeChat article** | L2 + `references/wechat-image-handling.md` (6-level pipeline) | Processing a WeChat article with channel triage + recursive browsing |
| **L3: Batch pipeline** | L2 + `references/smartsheet-sources.md` + `references/incremental-persistence.md` + `state.py` + `deduplicate.py` | Processing dozens of URLs from Smartsheet |
| **L2ac: Login + anti-crawl** | L2 + `references/anti-crawl-guide.md` + `references/site-adapters.md` + `scripts/check_login.py` + `scripts/login.py` + `scripts/crawl.py` | Target site requires login or is anti-crawled (liepin/58/nowcoder/baidu/moka) |

Each script works standalone at its level. `deduplicate.py` contains its own embedded
normalizer so L3 doesn't require running L1 first - but L1 is available as a lighter
tool when you just need to normalize one title for comparison.

## Scripts

- `scripts/normalize.py` - **L1**: Standalone title/company/JD-body normalization + `core_hash`
- `scripts/browse.py` - **L2**: Playwright-based page renderer and text extractor
- `scripts/validate.py` - Schema validator + packaging keys + evidence quality checks
- `scripts/deduplicate.py` - **L3**: Normalize, semantic-dedup, and merge candidates across batches
- `scripts/state.py` - **L3**: Incremental state manager: init, check (skip/extract), mark, diff
- `scripts/ocr_image.py` - Multi-backend OCR (vision -> PaddleOCR -> Tesseract) for image extraction
- `scripts/check_login.py` - **L2ac**: per-site login status + anti-crawl status health check (writes `store/state/health.json`)
- `scripts/login.py` - **L2ac**: interactive one-time login (QR/SMS/slider), persistent per-site browser profile
- `scripts/crawl.py` - **L2ac**: logged-in crawling (search/detail modes), browse.py-compatible output
- `scripts/anti_crawl_selftest.py` - **L2ac**: offline self-test of the anti-crawl layer (no network needed)

## PEV adapter boundary

When this package is activated by the backend PEV runtime, the deterministic
adapter tools are selected by the runtime but their business order is defined
here. For a named recruitment data source, query it with
`query-career-sheet-records` using the stated recency/location/company filters,
then fetch each returned `apply_url`; do not pass role keywords to a company
ledger. If the query returns no records, a bounded `search-public-job-pages`
fallback is allowed. Never retry an identical query or fetch, and treat login,
captcha, anti-bot, and empty/shell pages as honest evidence limits requiring
manual review rather than bypass attempts.

If a downstream activated Skill needs a target JD and the user supplied a role,
company, or public source constraint but no exact JD, this Skill may be planned
as the evidence-producing preceding step. It should capture one or more usable
public artifacts for the downstream step; it must not fabricate a JD or ask for
server-held candidate facts.
