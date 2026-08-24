# Single-URL Extraction Workflow (Planner -> Executor -> Verifier)

Load this document on demand when you are given ONE career-site URL and must
return the structured JDs for that company. It is the per-URL counterpart to
`SKILL.md` (which documents the SmartSheet batch workflow - you do NOT need
`SKILL.md` here).

The design solves two failure modes that a single-pass extractor hits:

1. **Output-cap loss.** One LLM generation can emit only ~8192 tokens, so a site
   with 151 jobs (e.g. Mioffice/xiaomi) loses ~90% of its listings if one agent
   tries to emit them all at once. **Fix:** extract per page, one small
   generation per page, persisted to disk - never emit the full set in one
   message.
2. **Context bloat.** Holding 16 pages of rendered text in one agent's context
   wastes tokens and slows it. **Fix:** stash each page's text on disk
   (`browse.py` writes `output/evidence/pages/page_NN.txt`) and let a
   per-page sub-agent read only its own page.

## Roles

- **You (planner/verifier).** Browse once, fan out one `jd_extractor` sub-agent
  per page, then merge with `deduplicate.py`. You never hold all the JDs - you
  hold only page-file paths and short write confirmations.
- **`jd_extractor` sub-agent.** Reads ONE page file, extracts that page's JDs
  as a JSON array, and persists them to `output/candidates/page_NN.json` via
  `write_candidates`. One sub-agent per page. Sub-agents do NOT dispatch
  further sub-agents (max depth 2).

## Step-by-step

### 1. Load the schema (once)
```
read_file(file_path="/job-discovery/references/schema.md", limit=1000)
```
Do NOT read `SKILL.md` - it is large and documents the SmartSheet flow.

### 2. Render + paginate (planner)
```
run_skill_script(script="browse", cli_args="<URL> --mode parallel-fetch --max-pages 20 --concurrency 4 --wait 800 --out output/evidence")
```
`parallel-fetch` (v1.6) is the default first call. Use `--wait 800` for the
normal public-site path: it is long enough for a post-navigation render but
avoids multiplying the CLI's conservative 3-second default by every bounded
public detail URL. If the page is thin or incomplete, the existing one retry
uses its normal wait. It detects URL-keyed
pagination (click next -> read URL -> click prev -> read URL -> diff to find the
`current`/`limit` params), pre-computes every page URL, and fetches them
**concurrently** via a thread pool (`--concurrency 4` = 4 worker browsers, the
Java-thread-pool analog). The result JSON carries **`page_files`** (paths to
`output/evidence/pages/page_NN.txt`), **`page_count`**, and **`used_path`**.
Process every returned page file. If a site reaches the configured
`--max-pages` safety ceiling before browse reports a positive terminal marker,
the run is incomplete and must be marked `needs_manual_review`, not complete.

`used_path` tells you which path it took:
- `"parallel"` - URL-keyed site, pages fetched concurrently. Proceed to step 3.
- `"click_fallback_no_detect"` / `"click_fallback_fetch_error"` - not URL-keyed
  (load-more / next-button style); it fell back to serial `click` internally.
  If `page_count > 1`, the click fallback paginated successfully - proceed to
  step 3 with those page files. If `page_count == 1` and `[PAGE_TEXT]` is thin,
  treat as the SPA case below.
- `"spa_shell_no_pagination"` - the page rendered < 500 chars (mokahr / feishu
  card-SPA shell). Retry ONCE with `--mode search-interact` (below).

- If `[PAGE_TEXT]` is missing / `< ~500 chars` (common on Moka/feishu/zhiye
  SPAs, or when `used_path` is `spa_shell_no_pagination`), retry ONCE with:
  ```
  run_skill_script(script="browse", cli_args="<URL> --mode search-interact --max-pages 3 --out output/evidence")
  ```
  If still empty, the page is an SPA shell / dead URL - emit
  `{"status":"blocked","reason":"page did not render job content"}` and stop.
- Skip pagination entirely only if `parallel-fetch` already returned all the
  jobs and `used_path` is `parallel`/`click_fallback_*` with `page_count` >= 1.

**HARD LIMITS - do not flail:**
- At most ONE `parallel-fetch` call and ONE `search-interact` retry per URL.
  (`parallel-fetch` internally already covers the `list` + `click` paths and
  their fallbacks - do NOT also issue separate `--mode list` or `--mode click`
  calls.) If `parallel-fetch` returns `page_count == 1` with thin text and
  `search-interact` is also empty, STOP and proceed to step 3 with whatever
  (if any) pages you have, or emit the blocked JSON. Do NOT loop on browse variants.
- NEVER `read_file` / `ls` / `glob` anything under `output/evidence/` - and
  especially never read a `.png`/`.jpg` screenshot. The evidence dir holds the
  content-addressed cache (often 0-byte text files or PNG screenshots); reading
  them returns empty/image bytes that the API rejects (400 crash). The page
  text you need is ONLY ever under the browse result's `[PAGE_TEXT]` marker, or
  via `read_evidence` on `output/evidence/pages/page_NN.txt` (which is a
  script, not `read_file`, and returns clean text).

After this step you have `page_files = [page_01.txt, ..., page_NN.txt]`.

### 3. Fan out one `jd_extractor` per page (executor, PARALLEL)
In your **next single message**, emit one `task` tool call per page file - all
in that one message so they run in parallel:

```
task(subagent_type="jd_extractor",
     description="Page file: output/evidence/pages/page_01.txt. Company: <COMPANY>.
                  Write your extracted candidates to output/candidates/page_01.json.")
task(subagent_type="jd_extractor",
     description="Page file: output/evidence/pages/page_02.txt. Company: <COMPANY>.
                  Write your extracted candidates to output/candidates/page_02.json.")
... one per page ...
```

Each sub-agent reads its own page file (it does NOT receive the page text from
you - this keeps your context lean) and writes its own output file. The
`task` result you get back is a short write confirmation, NOT the candidates.

If `page_count == 1` you still dispatch one `jd_extractor` (consistency).

### 3.1 Verify expected page cardinality when browse proves it

For URL-keyed `parallel-fetch`, browse may return all three of
`pagination.size_val`, `pagination.declared_total_pages`, and `page_count`.
When present, these are deterministic listing evidence, not model estimates:

- each non-final page must write exactly `size_val` candidates;
- the final page must write `declared_total - size_val * (page_count - 1)`;
- a smaller nonzero `written` count is a failed page, not a partial success.

Re-dispatch only each deficient page once, using the same evidence file and
`--append` output path. This recovers a single omitted card without re-crawling
the site or reprocessing every page. If it remains short after that retry,
preserve the artifacts and report `needs_manual_review`.

### 4. Merge + verify (verifier)
Once all sub-agents return, merge the per-page files into one deduplicated,
packaged, verified result:
```
run_skill_script(script="deduplicate",
                 cli_args="output/candidates/*.json --out output/candidates_merged.json")
```
`deduplicate.py` normalizes, drops title-only echoes of full JDs, adds
idempotency/similarity keys, and runs evidence-quality checks. Its stdout
summary reports `input_count` / `output_count` / `duplicates_removed`.

### 5. Coverage gate + final message (short - do NOT re-emit the candidates)

Run the deterministic gate after deduplication. Pass every returned page file
and a terminal marker **only if browse actually observed it** (disabled next
button, exhausted finite page range, or an equivalent positive end marker):

```
run_skill_script(script="coverage_gate",
                 cli_args="output/candidates_merged.json --pages <all page_files> --terminal-evidence <observed marker>")
```

If the gate rejects the output, preserve the candidates but report
`"status":"needs_manual_review"` with its reasons; do not call the run
complete. A missing terminal marker is therefore an explicit quality failure,
not permission to guess that pagination ended. Call the gate only once: its
verdict is terminal for this run. Do not browse or re-dispatch extraction after
it, because that creates an unbounded cost loop without new evidence.

Your final message must be ONLY a small JSON summary, e.g.:
```
{"status":"done","pages":16,"candidates_file":"output/candidates_merged.json","merged_count":151,"terminal_evidence":"last_page_disabled","coverage_verified":true}
```
The harness reads `output/candidates_merged.json` off disk - re-emitting the
candidates here would just re-hit the output cap that this design exists to
avoid. If the page was a login/captcha/anti-bot wall, emit instead
`{"status":"blocked","reason":"<one short line>"}` and stop.

## Constraints
- Dispatch one task per discovered page; do not truncate a large site because
  of a fixed total-tool budget. Restrict browsing to one `parallel-fetch` plus
  one SPA retry, and give each failed/deficient page at most one parallel retry.
- Run helper scripts ONLY via `run_skill_script`. Allowed: browse, validate,
  normalize, deduplicate, ocr_image, state, read_evidence, write_candidates.
- Never bypass login / captcha / anti-bot. If blocked, emit the blocked JSON.
- Use the company name you are given for `company_name`.
- Campus / 提前批 / 校招 is the default `recruitment_type` unless the page says
  otherwise (社招 / 实习).
