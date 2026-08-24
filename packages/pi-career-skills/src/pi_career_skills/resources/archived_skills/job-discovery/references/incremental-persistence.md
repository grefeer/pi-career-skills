# Incremental Persistence Logic (核心存量逻辑)

Load this document on demand when running the **L3 batch pipeline** (processing
dozens of URLs from Smartsheet with `state.json` change detection). For single-URL
extraction (L2), you do NOT need this - see `single-url-extraction.md`.

## The question: re-extract or skip?

Each SmartSheet record has a URL and an `更新时间` (last-update timestamp). When
you re-scan the sheets, you face three scenarios:

| Scenario | Detection | Action |
|----------|-----------|--------|
| New URL (never seen before) | URL not found in `output/state.json` | Full pipeline: browse -> LLM extract -> save |
| URL already processed, update_time **unchanged** | URL + update_time match in `output/state.json` | **Skip entirely** - nothing changed |
| URL already processed, update_time **changed** | URL matches but update_time differs | Re-browse, but check content_hash first |

### Save units (what gets persisted to disk)

```
output/
├── state.json                          ← Master index (URL -> hash -> candidates)
├── evidence/
│   ├── sha256_<content_hash>.txt       ← Raw page text (immutable, content-addressed)
│   └── sha256_<content_hash>.png       ← Screenshot
├── candidates/
│   └── sha256_<content_hash>.json      ← Extracted candidates (immutable, content-addressed)
├── merged_final.json                   ← Latest merged+dropped output (overwritten each run)
└── errors.jsonl                        ← URLs that failed (append-only log)
```

**Key insight**: Candidates are keyed by **page content_hash**, not by URL. If two
different SmartSheet records point to the same career page (same content), they
produce the same content_hash and share one `candidates/*.json` file.

### state.json format

```json
{
  "source_sheets": {
    "fGOTkFoVohnQ": {
      "title": "27届提前批秋招信息汇总",
      "sheets": ["t00i2h", "tbVCvT"],
      "last_scanned": "2026-07-19T12:00:00"
    },
    "czGbCooFQHwb": {
      "title": "27届校招秋招实习内推合集",
      "sheets": ["tZW9Ng"],
      "last_scanned": "2026-07-19T12:00:00"
    }
  },
  "processed": {
    "<content_hash>": {
      "url": "https://xiaopeng.jobs.feishu.cn/s/Pycfxid-fok",
      "source_file_id": "fGOTkFoVohnQ",
      "source_sheet_id": "t00i2h",
      "record_ids": ["rec_abc", "rec_def"],
      "last_update_time": "1720000000000",
      "company": "小鹏集团",
      "extracted_at": "2026-07-19T12:05:00",
      "candidates_count": 9
    }
  }
}
```

### Full incremental workflow

```
Phase 1: Scan smartsheets for changes
  For each sheet (t00i2h, tbVCvT, tZW9Ng):
    smartsheet.list_records -> get all {record_id, url, company, update_time, ...}
    Cross-reference with state.json
    Build three lists:
      - SKIP:     URL + update_time unchanged
      - RENDER:   URL changed OR update_time changed
      - DEAD:     Record deleted from sheet (keep its candidates, just note it)

Phase 2: Browse (for RENDER list only)
  python scripts/browse.py <url> --mode list --out output/evidence
  -> If content_hash already in state.json -> evidence unchanged, SKIP LLM extraction
  -> If content_hash is NEW -> Go to Phase 3

Phase 3: LLM Extract (for new content_hashes only)
  Read evidence/<hash>.txt
  Extract JDs per schema
  Save to candidates/<hash>.json
  validate.py --package --verify

Phase 4: Merge & Deduplicate (every run)
  python scripts/deduplicate.py output/candidates/*.json --out output/merged_final.json
  -> Takes ALL candidates (old + new), normalizes, dedups, merges
  -> output/merged_final.json is the COMPLETE cumulative history - NOT a current
    snapshot. It includes deleted/offline jobs and old JD versions.
    For a current-only view, filter by latest content_hash per active SmartSheet record.

> **Key distinction**:
> - `merged_final.json` = historical audit trail (cumulative, prefers old content)
> - Current snapshot = filter merged_final to candidates whose evidence_refs
>   include the latest content_hash from active records, deduplicated by
>   `job_identity_key`.

Phase 5: Update state.json
  Write all processed entries back
```

### Q&A: Your three questions, answered

**Q1: URL + 更新时间 怎么判断当前url是新的？是需要更新的？**

实际是**三级防线**，不是单次比较：

```
SmartSheet记录.更新时间 (毫秒时间戳，如 "1720000000000")
         │
         ▼
┌─ 第一级：state.json 里查这个 URL ─────────────────────────┐
│  遍历 state.json['processed'] 找到 url 匹配的条目            │
│  ├── 找不到 -> 新 URL -> 【需要提取】                          │
│  └── 找到了 -> 比较 last_update_time                          │
│       ├── 相同 -> 记录没被编辑过 -> 【跳过】（exit 0）          │
│       └── 不同 -> 记录被编辑过 -> 进入第二级                    │
└────────────────────────────────────────────────────────────┘
         │ (update_time 不同)
         ▼
┌─ 第二级：browse.py 渲染页面 -> 计算 content_hash ───────────┐
│  ├── content_hash 命中缓存（页面没变）-> 【跳过 LLM 提取】     │
│  └── content_hash 是新值（页面真变了）-> 进入第三级            │
└────────────────────────────────────────────────────────────┘
         │ (content_hash 是新的)
         ▼
┌─ 第三级：LLM 全量提取 -> validate -> dedup 合并 ─────────────┐
│  新 candidates/<new_hash>.json + 旧 candidates/<old_hash>.json │
│  -> deduplicate.py 按 canonical identity 合并                  │
└────────────────────────────────────────────────────────────┘
```

**核心理解**: `更新时间` 是 Smartsheet 记录编辑时间（可能只是改了个错别字），不是招聘页面更新时间。它只是**触发检查**的信号。真正判断页面是否变了的是 `content_hash`（页面文本的 SHA-256）。

**举例**:
- 有人在 SmartSheet 里给公司A记录加了一条"备注"-> `更新时间` 变了
- `state.py check` 返回 "update_time changed" -> 触发第二级
- `browse.py` 渲染页面 -> 页面内容没变 -> `content_hash` 命中缓存 -> **浏览器都没启动就跳过了**
- 结果：零消耗（一个 shell 调用而已）

**Q2: 公司A原来10个岗位，现在新增到12个，旧的10个怎么办？要全量更新吗？**

**A: 全量重提取 + dedup 合并，旧的不丢失也不重复。** 一般情况下岗位 JD 不会变，这个假设是对的。但当前架构无法做"增量提取"--因为 `browse.py` 输出的是一个完整的页面文本 blob，LLM 无法只提取"新增的2个"而不看全部12个。

实际发生的过程：

```
旧 run:   candidates/hash_old.json  ─── 10 个岗位
                │
新 run:   页面新增2个岗位 -> content_hash 变了 -> browse.py -> 新页面文本
                │
          LLM 从新文本中提取出 12 个岗位 -> candidates/hash_new.json
                │
          deduplicate.py(hash_old.json + hash_new.json):
            ├── 岗位1-10: canonical identity 相同 -> _merge()
            │     • evidence_refs 追加新 hash（证明两次 run 都看到了）
            │     • 其他字段：旧值保留，新值仅在旧值为空时补充
            │     • 不会产生 20 条，只输出 1 条
            └── 岗位11-12: 新 identity -> 直接加入
                │
          最终: 12 条（不是 22 条）
```

**关键**: `_merge()` 是**保守合并**--只追加 evidence_refs，不覆盖已有字段。所以如果岗位1的 JD 在新页面里变了（虽然你说一般不会），旧值保留，新 evidence_ref 被追加用于审计追溯。

**能否优化（不做全量提取）？** 可以作为未来改进：在 LLM prompt 里传入上一轮的10个岗位 title 列表，指示"只提取不在这10个里的新岗位"。但这需要 LLM 有精确的匹配能力，且风险是漏掉 JD 内容微调后的岗位。当前的保守策略（全量 + dedup）更安全。

**Q3: 本 skill 的保存逻辑是什么？**

**A: 内容寻址 + 累积追加。** 详见上方的「Save units」和「state.json format」章节。核心三条：

1. **不可变存储**: evidence 和 candidates 文件以 content_hash 命名，一旦写入永远不变。同一个 hash 不会产生两份文件。
2. **累积不覆盖**: 每次 run 只新增 `candidates/<new_hash>.json`，永不动旧文件。`merged_final.json` 从全部历史 candidates 重新生成。不可能因重新运行而丢数据。
3. **state.json 是唯一可变文件**: 只追加/更新条目，不删除（除非手动清理）。它是下一轮增量对比的基线。

## Phase 5 - NORMALIZE & DEDUPLICATE: Merge and collapse

**L1 (lightweight):** Normalize individual titles or compute core hashes on demand -
no need to load all candidates:

```bash
# Quick title comparison
python scripts/normalize.py --title "AI Agent开发工程师【2027届】（深圳）"
# -> "aiagent开发工程师"

# Compute body hash for identity dedup
python scripts/normalize.py --hash \
  --resp "设计并实现LLM Agent系统..." \
  --req "本科及以上，熟悉LangChain..."
# -> core_hash: 3f8a2b...
```

**L3 (full pipeline):** After processing all URLs (or a batch), run the deterministic
post-processor:

```bash
# Single command: normalize, dedup, package keys, quality-check, merge
python scripts/deduplicate.py output/candidates/*.json --out output/merged_final.json
```

This handles capabilities the LLM cannot do:
- **NFKC normalization** of titles/companies (zero-width chars, full-width -> half-width)
- **Trailing qualifier stripping** (算法工程师（上海）-> 算法工程师 for identity comparison)
- **Semantic deduplication** by canonical identity (JD-body hash for full-JD, normalized title for title-only)
- **Title-substring clustering** - same `core_hash` but different suffix (算法工程师 vs 算法工程师-应届) merges into one; genuinely different roles (算法工程师 vs 算法研究员) with shared JD body stay separate
- **Title-only echo dropping** - list-page titles that echo an already-captured full-JD detail page are removed
- **Idempotency keys** (SHA-256 of canonicalized fields - safe for database upsert)
- **Evidence quality checks** (staleness: pre-2024 dates, vagueness: < 50 chars, non-JD text)
- **Coverage completeness report** (unique URLs with candidates vs total unique evidence pages)

## Phase 6 - PERSIST: Collect and report

After dedup, review and save final output:

```bash
# Count candidates
python -c "import json; d=json.load(open('output/merged_final.json')); print(f'Total: {len(d)}')"

# View stats from dedup run (piped from deduplicate.py output)
```

## State and resumability

The skill uses the filesystem as its state store with `output/state.json` as the
master index. You can stop at any point and resume later - the incremental logic
above ensures you only process what's changed.

```bash
# Check which URLs have been processed (fast, no browser)
python -c "import json; s=json.load(open('output/state.json')); print(f'Processed: {len(s[\"processed\"])} hashes')"

# List already-cached evidence files
ls output/evidence/  # each file is sha256_<content_hash>.txt

# List already-extracted candidates
ls output/candidates/  # each file is sha256_<content_hash>.json
```
