# Site Catalog — Known Career Site Patterns

This catalog describes common career site platforms, their typical DOM structure,
and extraction strategies. Use this as a reference during Phase 2 (classification)
and Phase 4 (extraction).

**Always prefer what you actually observe on the page over these patterns.**
Sites change their UI; this catalog is a starting point, not ground truth.

---

## 1. Moka (mokahr.com)

**URL pattern**: `*.mokahr.com/campus-recruitment/*` or `app.mokahr.com/*`

**Structure**:
- Job listings are typically rendered in a scrollable card grid
- Each card shows: title, department, location, recruitment type
- Clicking a card often opens a detail panel/drawer (SPA-style, no URL change)
- The detail panel contains: description_text, responsibilities, requirements

**Extraction approach**:
1. `browse.py --mode interact` clicks category headers (e.g., "X-STAR顶尖人才 共3个职位"),
   then attempts to click individual job cards. This captures job titles, locations, and
   departments from the list page (typically 300-1000 chars of text).
2. **Full JD extraction requires manual playwright interaction** because Moka's detail panel
   is a pure SPA drawer — no URL change, no dedicated detail page, no exposed JSON API.
   Use the playwright skill to interactively click cards and capture panel text:
   ```bash
   # Explore Moka interactively with the playwright skill
   "$PWCLI" open https://app.mokahr.com/campus-recruitment/sangfor/27944 --headed
   "$PWCLI" snapshot
   # Click category tab → snapshot → click job card → snapshot → extract panel text
   ```
3. If the site has a "社会招聘" (experienced hire) section in addition to "校园招聘",
   you may find a different URL pattern that lists all jobs with inline details.
4. Some Moka instances expose job IDs in the DOM; you can try constructing detail URLs
   like `.../campus-recruitment/sangfor/27944#/job/<id>` though this is not guaranteed.

**Example sites**: 深信服 (sangfor), many Chinese tech companies

---

## 2. Feishu / Lark Career (jobs.feishu.cn)

**URL pattern**: `*.jobs.feishu.cn/*`

**Structure**:
- List page with job cards in a vertical or grid layout
- Each card typically shows: title, location, department, job type
- Detail information (responsibilities, requirements) is usually visible on the
  list page itself, expanded inline or in a side panel
- Often uses infinite scroll (not traditional pagination) — `browse.py`'s
  scroll-to-load handles this

**Extraction approach**:
1. `browse.py --mode list --max-pages 3` is usually sufficient
2. Each card area in the text has a clear structure: title → location → JD
3. Look for section headers: "职位描述", "职位要求", "Responsibilities"

**Example sites**: 理想汽车, 小鹏集团

---

## 3. zhiye.com Platform

**URL pattern**: `*.zhiye.com/campus/jobs` or `*.zhiye.com/social/jobs`

**Structure**:
- Standard portal-style layout
- Job list in table or card format
- **Key quirk**: JDs are often hidden behind a click-to-expand mechanism
  - The list shows title + location + brief summary
  - Clicking a job title reveals full JD in the same page or a popup
- May have traditional pagination (page numbers)

**Extraction approach**:
1. `browse.py --mode list` may only capture summaries if JDs are collapsed
2. If the text output lacks detailed responsibilities/requirements:
   - Use the playwright skill to interactively click individual job entries
   - Or, try finding an "展开全部" / "查看详情" button before extraction
3. Some zhiye sites have API endpoints returning JSON — check browser devtools
   for XHR calls to `/api/position/list` or similar

**Example sites**: 中国兵器 (norincogroupzhaopin), 华海清科 (hwatsing1)

---

## 4. WeChat Official Account Articles (mp.weixin.qq.com)

**URL pattern**: `https://mp.weixin.qq.com/s/*`

**Structure**:
- Single article page with title, formatted content, images
- Job listings embedded in the article body as text, tables, or images
- Often contains: company intro → position list → qualifications → how to apply
- Can include email delivery instructions ("请将简历发送至...")

**Extraction approach**:
1. Do NOT use `browse.py` — the tencent-docs skill's ReadGZH proxy is faster
   and bypasses WeChat's anti-scraping measures
2. The ReadGZH proxy returns: title + full text content + image URLs
3. Extract jobs from the article text directly
4. If the article is image-heavy (job listings as screenshots), mark as
   `needs_manual_review` and note the limitation — OCR is available but limited

---

## 5. Custom React/Next.js SPAs

**URL pattern**: Varies widely

**Signs**:
- `<div id="__next">` or `<div id="__nuxt">` in HTML
- Heavy JavaScript rendering, blank page without JS
- API-driven: data loaded via XHR/Fetch, not in initial HTML
- Modern design with animations, lazy loading, infinite scroll

**Extraction approach**:
1. `browse.py --mode list` is essential — curl won't work
2. These sites often load job data via API calls. If `browse.py` text output
   seems thin, the site might need XHR interception (advanced mode not yet
   supported in browse.py)
3. Fallback: use the playwright skill interactively to:
   - Open the page, snapshot
   - Identify job cards and detail links
   - Click through to detail pages one by one

---

## 6. Static / Traditional Career Sites

**URL pattern**: Varies, often `*.com/careers` or similar

**Signs**:
- Job listings visible in `curl` output or first 4KB HTML
- Server-rendered HTML with clear job structures
- Traditional `<a href>` links to detail pages
- May use `<table>` for job listings

**Extraction approach**:
1. Try `curl` first — if the full job text is in the HTML, no browser needed
2. If job detail pages are separate, extract listing page URLs and process
   each detail page with `browse.py --mode detail`
3. These are the easiest sites to extract from

---

## Decision Checklist for Unknown Sites

When you encounter a site not in this catalog:

1. **Check the URL domain** — does it match any known pattern?
2. **Check the HTML (`curl | head -c 4096`)** — are job listings in the raw HTML?
3. **Check for `<div id="__next">` or similar** — is it a SPA?
4. **Try `browse.py`** — does the text output contain full JDs?
5. **Check for APIs** — does the browser devtools Network tab show JSON responses?
6. **Decide**:
   - Full JDs visible in `browse.py` text → extract directly
   - Only summaries visible → need to click into detail pages
   - Blocked / login wall → skip, mark for manual review
