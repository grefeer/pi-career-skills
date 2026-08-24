# NormalizedJobCandidate Schema

Each extracted job posting must conform to this structure. All fields are optional
unless marked **required**. Unknown or missing values should be `null` / omitted,
never filled with placeholder text.

## JSON Structure

```json
{
  "title": "string | null",
  "company_name": "string | null",
  "department": "string | null",
  "description_text": "string",
  "responsibilities": "string",
  "requirements": "string",
  "locations": ["string"],
  "recruitment_types": ["string"],
  "industries": ["string"],
  "apply_url": "string | null",
  "application_channel_json": "object | null",
  "deadline_text": "string | null",
  "referral_code": "string | null",
  "confidence": 0.0,
  "evidence_refs": [{"content_hash": "string", "url": "string"}],
  "normalization_warnings": ["string"]
}
```

## Field Definitions

### title *(required)*
The job title as displayed. Normalize common variations:
- "软件工程师（应届生）" → "软件工程师" (put qualification in recruitment_types)
- "Software Engineer - Backend" → "Software Engineer, Backend"
- "2026届-算法工程师" → "算法工程师"

### company_name *(required)*
Full company name in the language of the posting. Prefer the official name:
- "北京理想汽车有限公司" → "理想汽车"
- Avoid abbreviations unless they ARE the brand name (e.g., "NIO" is correct)

### department
Department, division, or business unit. Common values:
- "技术部", "研发中心", "市场部", "AI Lab", "R&D"

### description_text
The full raw job description text. No truncation. Include everything the page displays
about this role, even if it overlaps with responsibilities/requirements.

### responsibilities
What the person will **DO**. Use bullet-style content. Extract from sections labeled:
- "岗位职责", "工作内容", "Responsibilities", "What you'll do", "职位描述"

### requirements
What the person must **HAVE**. Skills, degrees, experience. Extract from sections labeled:
- "任职要求", "岗位要求", "Qualifications", "Requirements", "What you'll need"

**Important**: If the source page doesn't separate responsibilities from requirements
cleanly, do your best to split them. Note the ambiguity in `normalization_warnings`.

### locations
Array of work locations. Normalize city names:
- "北京市" → "北京"
- "上海/杭州" → ["上海", "杭州"]
- "深圳（南山区）" → "深圳"
- "Remote" stays as "Remote"

### recruitment_types
Array of recruitment categories. **Use these standard values:**
- "校园招聘" — campus recruitment (应届生/毕业生)
- "社会招聘" — experienced hire
- "实习" — internship
- "博士专项" — PhD-specific program
- "提前批" — early/advance batch recruitment
- "内推" — internal referral
- "管培生" — management trainee
- "博士后" — postdoctoral

### industries
Array of industry tags if mentioned: "人工智能", "新能源汽车", "半导体", "互联网", etc.

### apply_url
Direct application link. If the listing page links to a detail page, use the detail page URL.
If there's a "立即投递" or "Apply" button, capture its href.

### application_channel_json
If the application requires specific channels (email, WeChat mini-program, specific portal):
```json
{
  "type": "email",
  "value": "hr@example.com",
  "subject_format": "姓名+学校+岗位"
}
```

### deadline_text
Application deadline as displayed. Preserve original format: "2026-03-31", "长期有效",
"招满即止", "Rolling basis".

### referral_code
Internal referral code if found in the Smartsheet source data or page content.

### confidence
0.0 to 1.0. How confident are you in this extraction?
- **0.9+**: Well-structured page, clearly labeled sections, no ambiguity
- **0.7-0.89**: Mostly clear but some fields required inference
- **0.5-0.69**: Significant inference needed, blended text, possible missing fields
- **<0.5**: Very unclear source — flag for manual review

### evidence_refs
References to the source evidence that supports this candidate:
```json
[
  {
    "content_hash": "sha256_abc123...",
    "url": "https://careers.example.com/jobs/123",
    "evidence_type": "browsed_detail_page"
  }
]
```

**evidence_type values** (determines confidence ceiling — see SKILL.md Phase 4):

| evidence_type | Source | Max confidence |
|--------------|--------|---------------|
| `browsed_detail_page` | browse.py rendered a single job detail page | 0.95 |
| `browsed_interact_page` | browse.py --mode interact: clicked cards, captured full JDs | 0.92 |
| `browsed_search_interact_page` | browse.py --mode search-interact: filtered + clicked cards | 0.92 |
| `browsed_list_page` | browse.py rendered a listing page with inline JDs | 0.90 |
| `browsed_search_page` | browse.py --mode search: keyword-filtered list page | 0.90 |
| `wechat_article_text` | WeChat article with plain-text JDs (via ReadGZH or direct) | 0.85 |
| `static_html` | Static HTML page, no JS rendering needed | 0.85 |
| `api_response` | Structured API response or JSON-LD | 0.80 |
| `ocr_full_jd_text` | OCR of an image containing complete JD body text | 0.75 |
| `ocr_text` | OCR of mixed text+image content (WeChat article images) | 0.60 |
| `ocr_poster_keyword` | OCR of a recruitment poster with titles/directions only | 0.55 |
| `browsed_list_page_title_only` | Listing page captured; details behind click wall | 0.75 |
| `manual_entry` | Manually entered or inferred candidate | 0.70 |
| `wechat_article_ocr` | WeChat article where OCR was used for image content | 0.70 |
| `ocr_poster_no_detail` | OCR produced company name + tags, zero JD body | 0.45 |

### normalization_warnings
Array of strings describing any normalization decisions the reviewer should know about:
```json
[
  "职责和要求在原文中未分开，由LLM分割",
  "地点从'长三角'推断为'上海'",
  "公司名称从域名推断，页面未明确显示",
  "来源为微信推文海报OCR提取，仅含方向标签无详细JD",
  "申请通过邮箱投递，无官网链接",
  "官网为SPA动态加载，browse.py未捕获岗位详情",
  "OCR URL可能存在识别错误，已人工修正"
]
```

## Validation Rules

- `title` and `company_name` must be non-empty strings
- `locations` must be a list of non-empty strings
- `recruitment_types` should use only standard values (non-standard values trigger a soft warning)
- `confidence` must be a number 0.0-1.0
- `evidence_refs` must contain at least one entry with a `content_hash`
- The root output is always a JSON array, even for single candidates
