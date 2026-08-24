# WeChat Image-Heavy Article Handling

> **TL;DR**: If an article has images, ALWAYS try OCR. Some WeChat articles are a single
> giant image with zero text. The decision tree below tells you exactly what to do at each
> step — read only as far as your situation requires.

---

## Level 1: Quick decision (30 seconds)

| You see this... | Do this |
|-----------------|---------|
| ReadGZH returns text > 200 chars, no images | ✅ Proceed with normal JD extraction |
| ReadGZH returns text > 200 chars, has images | → Go to **Level 2** (images may contain additional JDs) |
| ReadGZH returns text < 200 chars, has images | → Go to **Level 3** (this is an image-heavy article) |
| ReadGZH returns text < 200 chars, no images | ❌ Mark `needs_manual_review`, reason: "article has no content" |
| ReadGZH fails entirely | ❌ Mark `needs_manual_review`, reason: "ReadGZH proxy error" |

---

## Level 2: Text present but images exist

Some articles embed supplementary information in images (org charts, detailed JD tables,
QR codes for application channels). These images should be OCR'd to capture any additional
job information.

### Procedure

1. **List image URLs** from `fetch_wechat_article` output or ReadGZH raw response
2. **Download each image** (max 5, skip images < 10KB as they're likely icons/emojis)
3. **Run OCR** on each image:
   ```bash
   python scripts/ocr_image.py <image_file> --engine auto --out output/ocr
   ```
4. **Append OCR text** to the article body text with clear markers:
   ```
   [图片1 OCR内容]
   ...extracted text...
   ```
5. **Pass combined text** to the JD extraction LLM prompt
6. If any single image OCR produces a complete JD that was not in the article text, add
   it as an additional candidate with `evidence_type: "ocr_text"` in its evidence refs

---

## Level 3: Image-heavy article (little or no text)

This is the critical case. The article body is primarily or entirely images — common
patterns include:

- **Single long image**: One tall screenshot containing the entire job posting
- **Gallery of images**: Multiple images each containing a separate JD
- **Image table**: Job information formatted as a table within an image
- **Mixed**: Some header/footer text + image-based content

### Step 1: Download all images

```bash
# The ReadGZH proxy returns image URLs in the raw HTML.
# Extract them and download:
python -c "
from urllib.request import urlretrieve
# Replace with actual image URLs from ReadGZH response
image_urls = [...]  # from fetch_wechat_article or manual extraction
for i, url in enumerate(image_urls[:10]):
    urlretrieve(url, f'output/ocr/wechat_img_{i:02d}.png')
"
```

### Step 2: OCR every image

**Golden rule: Always attempt OCR. Never skip an image because it "looks" like it won't work.**

```bash
# OCR each downloaded image
for img in output/ocr/wechat_img_*.png; do
    echo "=== Processing: $img ==="
    python scripts/ocr_image.py "$img" --engine auto --out output/ocr
done
```

### Step 3: Determine outcome

| OCR result | Action |
|------------|--------|
| ≥1 image produced >100 chars of structured text | Proceed to JD extraction with combined OCR text |
| All images produced some text but <100 chars each | Mark `partial_success`, extract what you can, note low confidence |
| All OCR attempts produced zero text | Mark `needs_manual_review`, reason: "image-heavy article — OCR produced no usable text" |
| Vision-based OCR (pi-agent) produced readable text | Use pi-agent's vision output as the primary text source |

### Step 4: pi-agent vision fallback

When `scripts/ocr_image.py` returns `"engine": "vision_pending"`, this means PaddleOCR
and Tesseract are not installed, but pi-agent's built-in vision capability can read the
image directly:

1. Use the **read tool** to open the image file (supports `.png`, `.jpg`, `.webp`)
2. The LLM will receive the image as an attachment and can extract text
3. Structure the extracted text the same way as OCR output:
   ```
   [图片N OCR内容]
   <extracted job descriptions, company info, requirements, etc.>
   ```

This is the **most reliable method** when PaddleOCR is unavailable, since modern vision
models are excellent at reading Chinese text from screenshots.

---

## Level 4: Handling OCR results in the JD extraction pipeline

### Combining OCR text with article text

When you have both article body text and OCR results:

```
=== 文章正文 ===
<original article body text>

=== 图片1 OCR内容 (置信度: 0.87) ===
<OCR extracted text>

=== 图片2 OCR内容 (置信度: 0.73) ===
<OCR extracted text>
```

Feed this combined text to the JD extraction LLM prompt. The LLM will:
- Merge duplicate information across text and images
- Extract structured JDs from image-based tables
- Flag inconsistencies between text and image versions

### Evidence tracking

Each OCR result should be recorded in the candidate's `evidence_refs`:

```json
{
  "evidence_type": "ocr_text",
  "url": "https://mmbiz.qpic.cn/.../original_image_url",
  "content_hash": "sha256_<hash of image bytes>",
  "metadata": {
    "ocr_engine": "paddleocr",
    "ocr_confidence": 0.87,
    "image_dimensions": {"width": 1080, "height": 3200},
    "is_long_image": true
  }
}
```

### Confidence annotation

When JDs are extracted from OCR'd images, add to `normalization_warnings`:

```
"部分或全部内容来自图片OCR提取，置信度: 0.87"
```

---

## Level 5: Known failure modes

| Failure mode | Symptom | Recovery |
|-------------|---------|----------|
| Image is a scan of a printed document | OCR produces garbled text | Try pi-agent vision (better at handling scans) |
| Image contains mixed Chinese/English with special formatting | PaddleOCR misses English terms | Add `lang="ch+en"` or use vision fallback |
| Image is a complex table with merged cells | OCR output is flat text, structure lost | Mark `needs_manual_review`, note that table structure is critical |
| WeChat image CDN blocks direct download | HTTP 403 on image URLs | Use `browse.py` to screenshot the page instead, then OCR the full-page screenshot |
| Very long image (>5000px) | PaddleOCR runs out of memory or times out | Use vision-based OCR (better at handling large images) |

---

## Level 6: After OCR — Channel Triage & Recursive Browsing

> **This is the most important level for WeChat articles.** Having OCR text is only
> half the battle. The article is almost always an *index*, not the *source of truth* —
> the real JDs live at the career site it links to. This level tells you how to
> classify the application channel and recursively follow links to get full JDs.

### Step 1: Scan OCR text for channel signals

Read the full OCR output (or article body text) and look for these signals:

| Signal | Channel type | Examples |
|--------|-------------|----------|
| `@` followed by domain in text | **Email** (邮箱投递) | `hr@company.com`, `campus@example.cn` |
| Career site URL (`zhiye.com`, `mokahr.com`, `jobs.feishu.cn`, `campus.`, `/careers`) | **Official website** (官网投递) | `jereh.zhiye.com/campus`, `xiaopeng.jobs.feishu.cn/s/xxx` |
| `扫码` / `二维码` / QR code image, NO email, NO URL | **QR-code only** (扫码投递) | "扫描下方二维码投递简历" with no other channel |
| Both email AND career URL | **Mixed** (多渠道) | Has both `hr@company.com` AND `zhiye.com/campus` |
| No email, no URL, no QR mention | **Unknown** (无渠道) | Treat as QR-code only; mark `needs_manual_review` |

**Important**: OCR may corrupt URLs (e.g., `zhiye` → `zhlye`). If a URL pattern is
recognizable but garbled, try to reconstruct it: `*.zhiye.com/*`, `*jobs.feishu.cn/*`,
`*mokahr.com/*`. Flag the uncertainty in `normalization_warnings`.

### Step 2: Route by channel type

#### Channel A: Email (邮箱投递)

```
OCR text contains JD details? 
  ├─ YES (full JD body ≥ 200 chars) → Extract JDs from OCR text directly
  │     confidence: 0.60–0.70
  │     application_channel_json: {"type": "email", "value": "hr@company.com"}
  │     normalization_warnings: ["从微信推文OCR提取，申请通过邮箱投递"]
  │
  └─ NO (poster-style, keywords only) → Extract what you can (title + company + email)
        confidence: 0.35–0.50
        normalization_warnings: ["推文为海报/摘要形式，无详细JD，仅提取标题和投递邮箱"]
        evidence_type: "ocr_poster_keyword"
```

**No recursive browsing possible** — there's no URL to follow. Save the best you can.

#### Channel B: Official website (官网投递) — THE KEY PATH

This is where most value lives. The WeChat article is an index; the career site has the real JDs.

```
1. EXTRACT the career URL from OCR text
   ├─ Full URL found (e.g., "jereh.zhiye.com/campus") → use it directly
   ├─ Partial URL found → reconstruct (add https://, fix OCR errors)
   └─ No URL extractable → fall through to Channel D (Unknown)

2. BROWSE the career URL via browse.py
   python scripts/browse.py "<career_url>" --mode list --out output/evidence --max-pages 3
   
   ├─ SUCCESS: text_length ≥ 500 chars AND contains JD keywords (岗位/职位/要求/职责)
   │     → Go to Step 3 (extract detailed JDs from the browsed page)
   │
   ├─ PARTIAL: text_length ≥ 200 chars but NO JD keywords (navigation/footer only)
   │     → This is the SPA problem: zhiye.com, mokahr.com with dynamic loading
   │     → Fall back to OCR extraction + mark needs_deep_crawl
   │
   └─ FAILURE: text_length < 200 chars, or browse.py returns error/blocked
         → Fall back to OCR extraction + mark needs_deep_crawl

3. EXTRACT detailed JDs from the browsed career page
   ├─ Full JDs visible (responsibilities + requirements ≥ 100 chars each)
   │     → Replace OCR extraction with browsed extraction
   │     → confidence: 0.85–0.95
   │     → evidence_type: "browsed_detail_page" or "browsed_list_page"
   │
   └─ Only titles/overviews visible (list page with click-to-detail)
         → Extract what's available from list page
         → For each title, note that detail page needs separate click
         → confidence: 0.65–0.80
         → evidence_type: "browsed_list_page_title_only"
```

**Critical rule**: When recursive browsing succeeds, the browsed JD is **authoritative**
over the OCR summary. Keep the OCR as an additional evidence_ref, but use the browsed
page's content for responsibilities/requirements/confidence.

**SPA caveat (zhiye.com pattern)**: Career sites built on zhiye.com load job listings
via internal API calls. `browse.py --mode list` will capture the page shell (navigation,
category counts, footer) but NOT individual job cards. For these sites:
- Mark `needs_deep_crawl` — the positions exist but require playwright-level interaction
- Save the OCR-extracted titles + the career URL
- The skill-level pipeline stops here; a downstream worker can deep-crawl later

#### Channel C: QR-code only (扫码投递)

```
OCR text contains JD details?
  ├─ YES → Extract JDs, confidence 0.55–0.65
  │     normalization_warnings: ["仅支持扫码投递，无邮箱或官网链接"]
  │
  └─ NO → Extract titles + company only
        confidence: 0.30–0.45
        normalization_warnings: ["扫码投递+海报摘要，无详细JD", "需要人工补充投递方式"]
        needs_manual_review: true
```

#### Channel D: Mixed (多渠道)

Treat as **Channel B (官网)** first (recursive browsing is the richest source).
If browsing succeeds, add the email as an alternative channel in `application_channel_json`:

```json
{
  "application_channel_json": {
    "primary": {"type": "url", "value": "https://jereh.zhiye.com/campus"},
    "alternative": {"type": "email", "value": "campus@jereh.com"}
  }
}
```

### Step 3: Confidence tiers by data source

Use these confidence ranges when writing candidates. They are NOT suggestions — they
are calibration anchors that ensure downstream consumers can trust the data.

| evidence_type | Confidence range | When to use |
|--------------|-----------------|-------------|
| `browsed_detail_page` | 0.88 – 0.95 | Full JD from detail page rendered by browse.py |
| `browsed_list_page` | 0.80 – 0.88 | JDs visible inline on listing/search page |
| `browsed_list_page_title_only` | 0.60 – 0.75 | Only titles captured; details behind click wall |
| `ocr_full_jd_text` | 0.60 – 0.75 | OCR of an image that contained full JD body |
| `ocr_poster_keyword` | 0.40 – 0.55 | OCR of recruitment poster with only titles/directions |
| `ocr_poster_no_detail` | 0.30 – 0.45 | OCR produced company name + direction tags, zero JD body |

**Principle**: confidence reflects **data source quality**, not extraction effort.
A perfectly extracted keyword from a poster is still limited by its source — don't
inflate confidence above the tier ceiling.

### Step 4: Save and mark for deep-crawl

When recursive browsing fails (SPA, blocked, empty), append to `errors.jsonl`:

```json
{
  "url": "https://mp.weixin.qq.com/s/...",
  "career_url": "https://jereh.zhiye.com/campus",
  "status": "needs_deep_crawl",
  "reason": "Career site is SPA (zhiye.com) — job cards loaded via XHR, not captured by browse.py",
  "ocr_extracted_titles": ["AI应用", "控制算法", "智能制造"],
  "retry_strategy": "Use playwright skill to click into each category → each position → capture detail page text"
}
```

This creates a structured handoff: the skill's pipeline did everything it could
(OCR → channel triage → attempted browse), and the remaining work is scoped for
a downstream deep-crawl worker or manual playwright session.

---

## Quick reference: Command cheat sheet

```bash
# 1. Fetch article via ReadGZH (handled by tencent-docs skill)
# Output: article_title, article_text, image_urls[]

# 2. Download images
mkdir -p output/ocr
python -c "
import requests
image_urls = [...]  # paste from ReadGZH output
for i, url in enumerate(image_urls):
    r = requests.get(url, timeout=30)
    with open(f'output/ocr/img_{i:02d}.png', 'wb') as f:
        f.write(r.content)
"

# 3. OCR all images
for img in output/ocr/img_*.png; do
    python scripts/ocr_image.py "$img" --engine auto --out output/ocr
done

# 4. Combine results and extract JDs
# (done by the LLM in the JD extraction phase)
```

---

## Integration with SKILL.md workflow

In the main Job Discovery skill, the full WeChat article pipeline is:

```
URL → triage (wechat_article?)
    → browse.py --mode detail (renders page + screenshot)
    → Check text_length
       ├─ ≥ 200 chars → proceed with JD extraction (Level 1)
       └─ < 200 chars → image-heavy → OCR screenshot (Level 3, then Level 6)
    → Level 6: Channel Triage
       ├─ Email channel    → extract JDs from OCR, attach email, save
       ├─ Official URL     → browse.py on career URL
       │                     ├─ Success → extract detailed JDs (confidence 0.85+)
       │                     └─ Failure → save OCR JDs + mark needs_deep_crawl
       ├─ QR-code only     → extract what's available, mark needs_manual_review
       └─ Mixed/Unknown    → prefer URL path, fall back to OCR
    → Save candidates/<hash>.json
    → validate.py --package --verify
```

**Key insight**: This doc now covers the COMPLETE lifecycle from "how do I get text from
this image?" (Levels 1–5) through "what do I DO with the text?" (Level 6). The two
halves together turn a WeChat URL into structured JDs whether the source is text,
images, posters, or embedded career links.
