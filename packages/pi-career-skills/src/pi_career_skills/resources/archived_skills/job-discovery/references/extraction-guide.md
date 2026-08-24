# JD Extraction Guide

Best practices for extracting structured job descriptions from raw page text.
Read this before Phase 4 (Structure) to avoid common pitfalls.

---

## The Extraction Mindset

You are reading a wall of text from a career site and need to identify discrete
job postings, then structure each into `NormalizedJobCandidate`. The text may
be cleanly separated (one job per section) or completely blended (a single
long article listing 20 roles).

**Key principle**: A "job posting" = one distinct role. Different locations of
the same role are separate postings. Different departments for similar roles
are separate postings. Intern vs. full-time are separate postings.

---

## Step 1: Scan for Job Boundaries

Look for these signals that a new job posting begins:

- **Numbered lists**: "1. 算法工程师", "2. 前端开发工程师"
- **Section headers**: "【岗位一】", "## Software Engineer"
- **Repeating field patterns**: "岗位名称：", "Title:", "职位："
- **Location shifts**: "工作地点：北京" → new city = likely new posting
- **Recruitment type changes**: "校招岗位" vs "社招岗位"
- **Department headers**: "技术类", "产品类", "运营类" — each may contain multiple roles

If boundaries are unclear, err on the side of splitting rather than merging.
It's easier for a reviewer to merge two similar entries than to split a
combined one.

---

## Step 2: Extract Per-Job Fields

For each identified job posting, extract fields following the priority:

### Must-have (from page text)
1. **title** — Job title, first mention is usually canonical
2. **company_name** — From page header, domain, or prior Smartsheet metadata
3. **locations** — Any city/region mentioned with "工作地点" or similar
4. **responsibilities** — Under "岗位职责", "工作内容", "职位描述"
5. **requirements** — Under "任职要求", "岗位要求", "Qualifications"

### Good-to-have (from page text or Smartsheet metadata)
6. **department** — "所属部门", "Department"
7. **recruitment_types** — From context: "校招", "2026届", "提前批", "应届生"
8. **apply_url** — Direct link or detail page URL
9. **deadline_text** — "截止日期", "Application Deadline"

### Fill from Smartsheet prior metadata
If the Smartsheet `record_fields` contain information not visible on the page
(e.g., an internal referral code, a specific company name for a generic listing),
use those values and note it in `normalization_warnings`.

---

## Step 3: Separate Responsibilities from Requirements

This is the most common quality issue. Many Chinese career sites blend these.

**Responsibilities** (what you'll do):
- "负责...", "参与...", "设计...", "开发...", "优化..."
- "You will:", "Your role:", "Key responsibilities:"
- Action-oriented verbs describing the job itself

**Requirements** (what you need):
- "本科及以上学历", "硕士", "博士"
- "熟悉...", "精通...", "具备...经验"
- "X年以上...经验", "X+ years of experience"
- "计算机/电子/数学等相关专业"
- "熟练掌握Python/C++/Java"
- "Requirements:", "Qualifications:", "What you'll need:"

**When they're blended** (e.g., "我们需要一位精通Python的工程师负责后端开发"):
- Put "负责后端开发" in `responsibilities`
- Put "精通Python" in `requirements`
- Add to `normalization_warnings`: "职责和要求在原文中未显式分开，由LLM分割"

---

## Step 4: Normalize Values

### Job Titles
- Strip year/season prefixes: "2026届-算法工程师" → title="算法工程师", add "校园招聘" to recruitment_types
- Normalize English/Chinese: "Software Engineer / 软件工程师" → prefer the page's primary language
- Strip level suffixes unless they're essential: "高级Java开发工程师" → keep "高级" (it's part of the title)

### Locations
- Strip province/region suffixes: "北京市朝阳区" → "北京"
- Split combined locations: "北京/上海/深圳" → ["北京", "上海", "深圳"]
- Resolve ambiguous: "长三角" → ["上海"] (with normalization_warning)

### Confidence Scoring
- **0.95**: Well-labeled sections, unambiguous split between responsibilities/requirements
- **0.85**: Clear overall but some fields require minor inference
- **0.75**: Mixed text, had to separate responsibilities from requirements manually
- **0.60**: Heavy inference needed, significant missing fields
- **0.50**: Very unclear — flag for manual review

---

## Step 5: Common Pitfalls

1. **Merging different roles**: "嵌入式软件工程师" and "嵌入式硬件工程师" are different jobs
2. **Missing location**: If a page lists jobs for multiple cities but doesn't specify
   which job is where, note this in warnings rather than guessing
3. **Confusing "职位描述" with "公司介绍"**: The company introduction is NOT the job description
4. **Copying HTML artifacts**: Strip excessive whitespace, HTML entities, and navigation
   text that's not part of the JD
5. **Over-splitting**: "Python开发工程师" and "Python后端开发工程师" are probably the same role
   if they share the same description text
6. **Empty descriptions**: If the page only has titles and no descriptions, mark
   confidence low and note: "仅提取到岗位名称，页面未显示详细JD"

---

## Example

**Input text** (excerpt from a listing page):

```
理想汽车 2026届校园招聘 提前批

岗位一：具身智能算法工程师
工作地点：北京、上海
岗位职责：
1. 负责机器人感知、规划、控制算法的研究与开发
2. 参与VLA大模型的训练与部署
任职要求：
1. 计算机、自动化、电子工程等相关专业硕士及以上学历
2. 熟悉Python/C++，有ROS开发经验者优先
3. 在CVPR/ICCV/ECCV等顶会发表论文者优先
```

**Expected output**:

```json
[{
  "title": "具身智能算法工程师",
  "company_name": "理想汽车",
  "department": null,
  "description_text": "岗位一：具身智能算法工程师\n工作地点：北京、上海\n岗位职责：\n1. 负责机器人感知、规划、控制算法的研究与开发\n2. 参与VLA大模型的训练与部署\n任职要求：\n1. 计算机、自动化、电子工程等相关专业硕士及以上学历\n2. 熟悉Python/C++，有ROS开发经验者优先\n3. 在CVPR/ICCV/ECCV等顶会发表论文者优先",
  "responsibilities": "1. 负责机器人感知、规划、控制算法的研究与开发\n2. 参与VLA大模型的训练与部署",
  "requirements": "1. 计算机、自动化、电子工程等相关专业硕士及以上学历\n2. 熟悉Python/C++，有ROS开发经验者优先\n3. 在CVPR/ICCV/ECCV等顶会发表论文者优先",
  "locations": ["北京", "上海"],
  "recruitment_types": ["校园招聘", "提前批"],
  "industries": ["人工智能", "新能源汽车"],
  "apply_url": null,
  "application_channel_json": null,
  "deadline_text": null,
  "referral_code": null,
  "confidence": 0.95,
  "evidence_refs": [{"content_hash": "sha256_abc123", "url": "https://li.jobs.feishu.cn/s/jT9zJ28ajbw"}],
  "normalization_warnings": []
}]
```
