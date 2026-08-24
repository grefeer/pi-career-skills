# Smartsheet Sources & Phase 1 INGEST

Load this document on demand when you need to **read career URLs from the Tencent
Smartsheets** (the L3 batch source). For single-URL extraction (L2), you do NOT
need this - see `single-url-extraction.md`.

## Source SmartSheets (default)

This skill reads career URLs from two Tencent Smartsheet files. These are the
**canonical data sources** - always start by scanning them for new/updated records.

### Sheet A: 27届提前批秋招信息汇总（持续更新）

- **URL**: `https://docs.qq.com/smartsheet/DZkdPVGtGb1ZvaG5R?tab=t00i2h`
- **File ID**: `fGOTkFoVohnQ`
- **Title**: 27届提前批秋招信息汇总（持续更新）

| Sheet ID | Name | Visible | Records | Used for |
|----------|------|---------|---------|----------|
| `t00i2h` | 27届内推信息【重要】 | ✅ | ~780 | **Primary**: 内推 links + codes |
| `tbVCvT` | 27届招聘推文校招信息 | ❌ | ~639 | **Secondary**: 招聘推文 links |

**t00i2h field mapping** (fields relevant for extraction):

| Field | Type | Role in extraction |
|-------|------|--------------------|
| `企业名称` | text | -> `company_name` (primary source) |
| `内推链接` | url | -> `apply_url` (entry point for browsing) |
| `整体文案` | text | -> prior metadata for JD extraction context |
| `内推码(区分大小写)` | text | -> `referral_code` |
| `招聘类型` | select | -> `recruitment_types` hint |
| `行业类型` | select | -> `industries` hint |
| `工作地点` | select | -> `locations` hint |
| `答疑链接` | url | -> supplementary link (Q&A) |
| `更新时间` | dateTime | -> **change detection key** (millisecond timestamp) |

**tbVCvT field mapping**:

| Field | Type | Role in extraction |
|-------|------|--------------------|
| `企业名称` | text | -> `company_name` |
| `招聘链接` | url | -> `apply_url` |
| `整体文案` | text | -> prior metadata |
| `内推码` | text | -> `referral_code` |
| `更新日期` | dateTime | -> change detection key |

### Sheet B: 27届校招秋招实习内推合集（欢迎大家分享！）

- **URL**: `https://docs.qq.com/smartsheet/DY3pHYkNvb0ZRSHdi?tab=BB08J2`
- **File ID**: `czGbCooFQHwb`
- **Title**: 27届校招秋招实习内推合集（欢迎大家分享！）

| Sheet ID | Name | Records |
|----------|------|---------|
| `tZW9Ng` | 每日更新 | ~1079 |
| `BB08J2` | 实习内推汇总 | - |

**tZW9Ng field mapping**:

| Field | Type | Role in extraction |
|-------|------|--------------------|
| `公司名称` | text | -> `company_name` |
| `投递链接` | url | -> `apply_url` (entry point) |
| `招聘岗位` | text | -> title hint (vague in this sheet) |
| `整体文案` | text | -> prior metadata |
| `工作地点` | text | -> location hint |
| `招聘类型` | select | -> `recruitment_types` hint |
| `截止日期` | text | -> `deadline_text` |
| `更新时间` | dateTime | -> change detection key |

> **Note:** `BB08J2` (实习内推汇总) is listed as user reference but `tZW9Ng` (每日更新)
> is the primary daily-update sheet. The user mentioned `tab=BB08J2` in the URL
> but `BB08J2` maps to "实习内推汇总" - `tZW9Ng` is the one named "每日更新". Use
> both as needed.

## Phase 1 - INGEST: Collect URLs

Use the **tencent-docs skill** to read source data:

```bash
# List all tables in the smartsheet file
# (via mcporter: smartsheet.list_tables)

# Read records from the target sheet (e.g., "每日更新")
# (via mcporter: smartsheet.list_records with sheet_id and pagination)

# Save each record as a JSON line in output/tasks.jsonl:
# {"url": "...", "company": "...", "location": "...", ...record_fields}
```

The `record_fields` from Smartsheet are valuable prior metadata - they may contain
company names, locations, referral codes, and deadlines that the career page itself
doesn't display. Always carry them forward into the final candidate.
