from __future__ import annotations

import re

from pi_career_skills.business.common.job_strength import analyze_job_strength
from pi_career_skills.business.common.schemas import NormalizedJobCandidate
from pi_career_skills.business.common.taxonomy import taxonomy_tags

# --- Heading / section markers in Chinese and English ---

_TITLE_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:岗位名称|职位名称|招聘职位|Job Title)\s*[:：]?\s*(.+?)(?:\n|$)", re.IGNORECASE),
    re.compile(
        r"^(.{2,30}(?:岗|职位|工程师|开发|算法|研究员|架构师|科学家|设计师|"
        r"分析师|顾问|专家|运营|产品|经理|管培生|培训生|实习生|专员|助理|PMO|"
        r"engineer|developer|manager|analyst|designer|specialist|intern))",
        re.MULTILINE | re.IGNORECASE,
    ),
]

#: Navigation / chrome labels that a generic title pattern must never accept as
#: a job title.  List pages and detail pages both render these as ordinary
#: short lines, so a bare _TITLE_PATTERNS[1] scan would treat the first one as
#: the page's role.
_NAV_CHROME_TITLE_MARKERS: tuple[str, ...] = (
    "浏览职位",
    "查看全部",
    "查看详情",
    "查看更多",
    "立即应聘",
    "申请职位",
    "职位信息",
    "职位点评",
    "热门职位",
    "最新职位",
    "推荐职位",
    "职位收藏",
    "职位订阅",
    "更多职位",
    "更多筛选",
    "招聘观察",
    "职位搜索",
    "职位列表",
    "返回列表",
    "相似职位",
    "相关职位",
    "看了又看",
    "猜你喜欢",
)

_COMPANY_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:公司名称|公司|企业名称|招聘单位|employer|company)\s*[:：]?\s*(.+?)(?:\n|$)", re.IGNORECASE),
]

_DEPARTMENT_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:所属部门|部门|事业部|业务线|department|division)\s*[:：]?\s*(.+?)(?:\n|$)", re.IGNORECASE),
]

_RESPONSIBILITIES_HEADERS: list[re.Pattern] = [
    re.compile(r"(?:岗位职责|工作职责|职位描述|工作内容|主要职责|职责描述|岗位定位|你将负责|responsibilities|job description|what you.?ll do|key responsibilities)", re.IGNORECASE),
]

_REQUIREMENTS_HEADERS: list[re.Pattern] = [
    re.compile(r"(?:任职要求|任职资格|岗位要求|职位要求|资格要求|招聘要求|应聘条件|基本要求|专业要求|希望你是|requirements|qualifications|what you.?ll need|required skills|basic requirements)", re.IGNORECASE),
]

_LOCATION_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:工作地点|工作地址|上班地点|工作城市|地点|location|work location)\s*[:：]?\s*(.+?)(?:\n|$)", re.IGNORECASE),
]

# Each rule pairs a detection regex with the single normalized type it maps to.
# A type is appended at most once, so the ``type_name not in types`` guard is the
# only branch in ``_detect_recruitment_types`` and both its arms are reachable.
_RECRUITMENT_TYPE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:实习|intern|实习生)", re.IGNORECASE), "internship"),
    (re.compile(r"(?:校招|校园|应届|campus|graduate)", re.IGNORECASE), "campus_recruitment"),
    (re.compile(r"(?:社招|社会|全职|full.?time)", re.IGNORECASE), "full_time"),
]

_APPLY_METHOD_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:投递方式|申请方式|如何申请|how to apply|apply method)\s*[:：]?\s*(.+?)(?:\n|$)", re.IGNORECASE),
]

_DEADLINE_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:截止日期|截止时间|招聘截止|报名截止|deadline|closing date|expires?)\s*[:：]?\s*(.+?)(?:\n|$)", re.IGNORECASE),
]

_IGUOPIN_REQUIREMENTS_HEADERS: list[re.Pattern] = [
    re.compile(r"(?:任职资格|岗位要求|职位要求|资格要求)\s*[:：]?", re.IGNORECASE)
]

_PUBLISHED_AT_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"(?:发布时间|发布于|发表于|更新于|posted\s*(?:on|at)?|published\s*(?:on|at)?)\s*[:：]?\s*"
        r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![一-鿿\w])((?:\d+)\s*(?:年前|个月前|天前|小时前|分钟前))\s*(?:关注|$)"
    ),
]

_REFERRAL_CODE_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:内推码|推荐码|内推|referral code|referral)\s*[:：]?\s*(.{0,30})(?:\n|$)", re.IGNORECASE),
]

# --- B3: degree + priority structured extraction (FindJobs _normalize_degree) ---

#: Degree whitelist, most-specific first: ``学历不限`` must beat the bare
#: ``不限``, and a degree mention inside prose (``本科及以上``) is caught by
#: the keyword itself and normalized to the degree tier.
_DEGREE_RULES: list[tuple[str, str]] = [
    ("学历不限", "不限"),
    ("不限学历", "不限"),
    ("博士", "博士"),
    ("硕士", "硕士"),
    ("本科", "本科"),
    ("大专", "大专"),
]

#: Priority semantics: ``must`` beats ``preferred`` when both appear in one
#: JD (e.g. "必须具备本科以上学历，硕士优先" is a must-degree posting).
_PRIORITY_MUST_RE: re.Pattern = re.compile(
    r"(?:必须|必备|硬性要求|须具备|要求具备)", re.IGNORECASE
)
_PRIORITY_PREFERRED_RE: re.Pattern = re.compile(
    r"(?:优先|加分项|加分|preferred|plus)", re.IGNORECASE
)

# --- Multi-job page separators (Chinese numbered position markers) ---

_MULTI_JOB_SEPARATORS: list[re.Pattern] = [
    re.compile(r"\n\s*(?:岗位|职位)\s*(?:二|三|四|五|[2-5])\s*[：:]"),
    re.compile(r"\n\s*招聘岗位\s*(?:二|三|四|五|[2-5])"),
]

_TITLE_HEADER_RE: re.Pattern = re.compile(r"^(?:岗位名称|职位名称|招聘职位)", re.MULTILINE)

#: Hard ceiling on candidates produced from one page. Card-list portals
#: (Feishu careers) segment into dozens of openings, so the cap is a reachable
#: guard rather than the old unreachable 10-limit.
_MAX_CANDIDATES_PER_PAGE = 100

# Feishu-style career portals render every opening as a card whose first line
# is the job title and whose second line is a dense meta line carrying the
# ``职位 ID`` marker (with locations and recruitment type inline). Splitting on
# title+meta pairs lets a whole listing (e.g. 61 NIO agent roles) extract as
# individual candidates instead of one undifferentiated blob. The role token
# may sit anywhere in the title (real titles trail suffixes such as
# ``（AI安全方向）`` or ``-NOMI``), with at most 30 chars of trailing detail;
# a chrome line above a meta (e.g. ``推荐投递``) carries no role token and is
# therefore never misread as a title.
_CARD_TITLE_ROLE_SUFFIXES = (
    "工程师|开发|算法|研究员|架构师|科学家|设计师|分析师|顾问|专家|"
    "运营|产品|经理|管培生|培训生|实习生|专员|助理|PMO"
)
_CARD_LIST_SPLIT_RE: re.Pattern = re.compile(
    rf"(?m)^(?P<title>.{{2,60}}?(?:{_CARD_TITLE_ROLE_SUFFIXES}).{{0,30}}?)\n"
    r"(?P<meta>[^\n]*职位\s*ID[^\n]*)$"
)

# Iguopin (国聘) search listings render every opening as a card whose title
# line is followed by a corner-bracket city block (``「城市」``), then a dense
# salary/type/experience/degree line and the company block. No ``职位 ID``
# meta line, no 【】 block -- a third distinct listing layout. ``city`` may be
# empty (malformed bracket) and still anchors the split.
_IGUOPIN_CARD_SPLIT_RE: re.Pattern = re.compile(
    rf"(?m)^(?P<title>.{{2,60}}?(?:{_CARD_TITLE_ROLE_SUFFIXES}).{{0,30}}?)\n"
    r"「\s*(?P<city>[^「」\n]{0,30})\s*」"
)

# JAKA-style career lists render every opening as ``title`` followed by a
# small vacancy count and a location line. There is no bracket block or job-id
# marker, so the other card splitters cannot identify the individual JDs.
_COUNT_CARD_SPLIT_RE: re.Pattern = re.compile(
    rf"(?m)^(?P<title>.{{2,60}}?(?:{_CARD_TITLE_ROLE_SUFFIXES}).{{0,30}}?)\n"
    r"(?P<count>\d{1,3})\n"
    r"(?P<city>[^\n]{1,40})$"
)

# Meituan's rendered campus feed uses a title/type/city/update tuple before
# each inline JD. The page contains the actual responsibilities for hundreds
# of roles, so treating it as one blob loses both the requested role and its
# update date even though the public evidence is complete.
_UPDATED_JOB_CARD_SPLIT_RE: re.Pattern = re.compile(
    rf"(?m)^(?P<title>.{{2,80}}?(?:{_CARD_TITLE_ROLE_SUFFIXES}).{{0,30}}?)\n"
    r"(?P<recruitment>日常实习|转正实习|应届校招|社会招聘|社招|校招)\n"
    r"(?P<city>[^\n]{2,40})\n"
    r"更新于(?P<updated>\d{4}[-/.]\d{1,2}[-/.]\d{1,2})$"
)

# Liepin-style portals render every opening as a card whose title line is
# immediately followed by a bracket-wrapped city block (``【\n城市\n】``),
# then salary / experience / education / benefits / company / recruiter
# lines. There is no ``职位 ID`` meta line, so ``_CARD_LIST_SPLIT_RE`` never
# fires and a whole listing (e.g. a "本期新增 2997 个职位" feed) degrades to
# one page-level blob. Splitting on title + city-bracket pairs turns the feed
# into per-opening segments. ``city`` is optional: a malformed empty bracket
# (``【】``) still splits at the title and simply loses the location.
_LIEPIN_CARD_SPLIT_RE: re.Pattern = re.compile(
    rf"(?m)^(?P<title>.{{2,60}}?(?:{_CARD_TITLE_ROLE_SUFFIXES}).{{0,30}}?)\n"
    r"【\s*(?P<city>[^】\n]{0,20})\s*】"
)


def _card_meta_cities(meta: str) -> str | None:
    """Read the city list leading a Feishu card meta line, if present.

    The meta line begins with locations, either directly
    (``北京、上海校招正式...``) or with a count
    (``武汉、合肥、上海等 4 个城市校招正式...``); the capture is guarded so a
    non-city lead such as ``本科及以上校招...`` or a digit-led
    ``2027届校招...`` is rejected rather than emitted as a location.
    """
    m = re.match(
        r"^([一-鿿·、等]{2,20}?)(?:\s*\d+\s*个城市)?(?:校招|社招|实习)", meta
    )
    if m is None:
        return None
    candidate = m.group(1)
    if candidate.endswith("等"):
        candidate = candidate[:-1]
    if (
        "、" not in candidate
        and len(candidate) > 4
        and not candidate.endswith(("市", "省", "都", "州"))
    ):
        return None
    return candidate


def _normalize_card_segment(card_text: str, match: re.Match, segment_start: int) -> str:
    """Prefix one Feishu-style card with extractable title/location headers.

    The injected ``职位名称：`` / ``工作地点：`` lines reuse the labeled
    extraction patterns (``_TITLE_PATTERNS[0]``, ``_LOCATION_PATTERNS``), so
    per-card titles and cities surface without touching the heuristics that
    other page layouts rely on. ``match.start()`` is absolute in the source
    page text; ``card_text`` is the sliced segment, so the offset is rebased.
    """
    title = match.group("title").strip()
    header = f"职位名称：{title}\n"
    cities = _card_meta_cities(match.group("meta"))
    if cities is not None:
        header += f"工作地点：{cities}\n"
    return header + card_text[match.start() - segment_start:].strip()


def _normalize_bracket_card_segment(
    card_text: str, match: re.Match, segment_start: int
) -> str:
    """Prefix one bracket-city card (Liepin 【】 / Iguopin 「」) with headers.

    Both layouts render each opening as ``title`` followed by a city block --
    Liepin wraps it in full-width brackets (``【\n城市\n】``), Iguopin in
    corner brackets (``「城市」``). Injecting the same labeled headers as
    ``_normalize_card_segment`` (``职位名称：`` / ``工作地点：``) reuses
    ``_TITLE_PATTERNS`` / ``_LOCATION_PATTERNS`` without touching the
    Feishu-specific meta parser. The city captured by the split regex may be
    empty (malformed bracket), in which case only the title header is emitted.
    """
    title = match.group("title").strip()
    header = f"职位名称：{title}\n"
    city = match.group("city").strip()
    if city:
        header += f"工作地点：{city}\n"
    return header + card_text[match.start() - segment_start:].strip()


def _normalize_count_card_segment(
    card_text: str, match: re.Match, segment_start: int
) -> str:
    """Prefix one title/count/location card with extractable headers."""
    title = match.group("title").strip()
    header = f"职位名称：{title}\n"
    city = match.group("city").strip()
    if re.fullmatch(r"[一-鿿·、\-]{2,40}", city):
        header += f"工作地点：{city}\n"
    return header + card_text[match.start() - segment_start:].strip()


def _normalize_updated_job_card_segment(
    card_text: str, match: re.Match, segment_start: int
) -> str:
    """Prefix one rendered title/type/city/update card with labeled fields."""
    header = f"职位名称：{match.group('title').strip()}\n"
    city = match.group("city").strip()
    if city:
        header += f"工作地点：{city}\n"
    return header + card_text[match.start() - segment_start:].strip()


def _split_multi_job_page(text: str) -> list[str]:
    """Split page text containing multiple job postings into segments.

    Detects Chinese multi-job separators like 岗位二： / 职位2：
    or repeated title headings (岗位名称 appearing twice), or Feishu-style
    card listings (title line followed by a ``职位 ID`` meta line - each card
    becomes its own segment), or bracket-city card feeds (title line followed
    by a ``【城市】`` Liepin block or a ``「城市」`` Iguopin block, at least
    two cards). Returns the detected segments, each fed separately to
    extraction.
    """
    if not text.strip():
        return [text]

    # Pattern 1: Numbered position markers
    for pattern in _MULTI_JOB_SEPARATORS:
        m = pattern.search(text)
        if m:
            split_pos = m.start()
            before = text[:split_pos].strip()
            after = text[split_pos:].strip()
            result = []
            if before:
                result.append(before)
            # ``after`` always begins at the matched ``\n`` and therefore always
            # contains the separator marker (e.g. ``岗位二：``), so its falsy
            # arm is unreachable.
            if after:  # pragma: no cover
                result.append(after)
            return result[:2]

    # Pattern 2: Repeated title headers (e.g. 岗位名称 appearing twice)
    heading_matches = list(_TITLE_HEADER_RE.finditer(text))
    if len(heading_matches) >= 2:
        segments = []
        for i, m in enumerate(heading_matches):
            start = m.start()
            end = heading_matches[i + 1].start() if i + 1 < len(heading_matches) else len(text)
            segments.append(text[start:end].strip())
        return segments[:2]

    # Pattern 3: Feishu-style card listings. Every card becomes its own
    # segment; header/navigation chrome between cards is dropped because it is
    # not a job posting.
    card_matches = list(_CARD_LIST_SPLIT_RE.finditer(text))
    if card_matches:
        segments = []
        for i, m in enumerate(card_matches):
            start = m.start()
            end = card_matches[i + 1].start() if i + 1 < len(card_matches) else len(text)
            segments.append(_normalize_card_segment(text[start:end], m, start))
        return segments

    # Pattern 4/5: bracket-city card feeds (Liepin 【】 / Iguopin 「」). At
    # least two cards are required: a lone ``title + [城市]`` pair is
    # indistinguishable from a normal single JD page that mentions a bracketed
    # location, which must stay on the unchanged single-segment path.
    bracket_matches = list(_LIEPIN_CARD_SPLIT_RE.finditer(text))
    if not bracket_matches:
        bracket_matches = list(_IGUOPIN_CARD_SPLIT_RE.finditer(text))
    if len(bracket_matches) >= 2:
        segments = []
        for i, m in enumerate(bracket_matches):
            start = m.start()
            end = (
                bracket_matches[i + 1].start()
                if i + 1 < len(bracket_matches)
                else len(text)
            )
            segments.append(_normalize_bracket_card_segment(text[start:end], m, start))
        return segments

    # Pattern 3b: rendered title/type/city/update feeds. Require two cards to
    # avoid reinterpreting an ordinary detail page with an update label.
    updated_card_matches = list(_UPDATED_JOB_CARD_SPLIT_RE.finditer(text))
    if len(updated_card_matches) >= 2:
        segments = []
        for i, match in enumerate(updated_card_matches):
            start = match.start()
            end = (
                updated_card_matches[i + 1].start()
                if i + 1 < len(updated_card_matches)
                else len(text)
            )
            segments.append(
                _normalize_updated_job_card_segment(text[start:end], match, start)
            )
        return segments

    # Pattern 6: title + vacancy-count + location cards (JAKA). Require at
    # least two cards so an ordinary JD containing a standalone number is not
    # reinterpreted as a listing.
    count_matches = list(_COUNT_CARD_SPLIT_RE.finditer(text))
    if len(count_matches) >= 2:
        segments = []
        for i, match in enumerate(count_matches):
            start = match.start()
            end = (
                count_matches[i + 1].start()
                if i + 1 < len(count_matches)
                else len(text)
            )
            segments.append(
                _normalize_count_card_segment(text[start:end], match, start)
            )
        return segments

    return [text]


def _is_chrome_title(title: str) -> bool:
    """Return True when a candidate title is page chrome rather than a role."""
    return any(marker in title for marker in _NAV_CHROME_TITLE_MARKERS)


def _extract_title(text: str) -> tuple[str | None, float]:
    """Extract job title from text using keyword heuristics."""
    portal_detail = re.search(
        r"^职位详情页\s*\n\s*([^\n]{2,80})\s*$", text, re.MULTILINE
    )
    if portal_detail:
        return portal_detail.group(1).strip(), 0.9
    for index, pattern in enumerate(_TITLE_PATTERNS):
        if index == 0:
            m = pattern.search(text)
            if m:
                title = m.group(1).strip()
                if 1 <= len(title) <= 80 and not _is_chrome_title(title):
                    return title, 0.7
            continue
        # Generic line pattern: scan every match and take the first non-chrome
        # line, so a nav bar at the top of the page ("浏览职位") never wins
        # over the real role heading that follows.
        for m in pattern.finditer(text):
            title = m.group(1).strip()
            if 1 <= len(title) <= 80 and not _is_chrome_title(title):
                return title, 0.7
    return None, 0.0


# Values that a broad ``公司`` label captures when the page's line is a
# section marker rather than the employer itself (e.g. WatchJobs renders
# ``公司情况`` followed by a blurb). None of these is a real company name.
_COMPANY_NOISE_SUFFIXES = (
    "情况",
    "简介",
    "介绍",
    "规模",
    "性质",
    "类型",
    "行业",
    "地址",
    "官网",
    "全称",
    "名称",
    "所在",
    "主页",
)


def _is_company_noise_value(value: str) -> bool:
    """True when a captured ``公司`` label value is page chrome, not an employer."""
    if not value:
        return True
    stripped = value.strip()
    if len(stripped) < 2:
        return True
    return any(stripped.startswith(suffix) for suffix in _COMPANY_NOISE_SUFFIXES)


def _extract_company(text: str) -> tuple[str | None, float]:
    """Extract company name from text using keyword heuristics."""
    nowcoder_document_title = re.search(
        r"^[^_\n]+_([^_\n]{2,40}?)(?:校招|社招|招聘|实习|内推)_牛客网\s*$",
        text,
        re.MULTILINE,
    )
    if nowcoder_document_title:
        return nowcoder_document_title.group(1).strip(), 0.9
    for pattern in _COMPANY_PATTERNS:
        m = pattern.search(text)
        if m:
            company = m.group(1).strip()
            if _is_company_noise_value(company):
                continue
            if 1 <= len(company) <= 100:
                return company, 0.7
    return None, 0.0


def _extract_department(text: str) -> str | None:
    """Extract department from text."""
    for pattern in _DEPARTMENT_PATTERNS:
        m = pattern.search(text)
        if m:
            dept = m.group(1).strip()
            if 1 <= len(dept) <= 80:
                return dept
    return None


def _extract_section(text: str, header_patterns: list[re.Pattern]) -> str:
    """Extract content under a section heading.

    Finds the first matching header, then captures text up to the next
    section heading (a line with common heading keywords) or end of string.
    """
    for pattern in header_patterns:
        m = pattern.search(text)
        if m:
            start = m.end()
            # Look for next section header to delimit this section
            remainder = text[start:]
            # Some rendered portals emit both a tab label and the page's own
            # identical heading (``岗位职责\n岗位职责：``). Skip that immediate
            # duplicate so the delimiter scan does not return an empty body.
            leading = remainder.lstrip(" \t\r\n：:")
            for same_header in header_patterns:
                duplicate = same_header.match(leading)
                if duplicate is not None:
                    remainder = leading[duplicate.end():].lstrip(" \t\r\n：:")
                    break
            # Find the next line that looks like a heading
            next_header = re.search(
                r"\n\s*(?:岗位职责|工作职责|职位描述|任职要求|岗位要求|"
                r"职位要求|资格要求|任职资格|专业要求|工作地点|投递方式|截止日期|"
                r"希望你是|竞争力分析|单位信息|公司介绍|公司简介|关于我们|responsibilities|"
                r"requirements|qualifications|location|about us)\s*[:：]?\s*\n",
                remainder,
                re.IGNORECASE,
            )
            section_text = (
                remainder[: next_header.start()] if next_header else remainder
            )

            # Clean up. The capture begins right after the heading label, so it
            # may still lead with the label's colon/whitespace (the heading
            # pattern does not consume ``:``) - strip those separators first.
            section_text = re.sub(r"\s+", " ", section_text.lstrip("：:、，")).strip()
            if len(section_text) > 10:
                return section_text
    return ""


# Values a broad ``地点`` label captures when the line is a section marker
# rather than a city -- e.g. WatchJobs renders ``AI 推测地点`` followed by
# ``共 2 处``. ``共`` / ``推测`` / ``待定`` are never real locations.
_LOCATION_NOISE_MARKERS = ("共", "推测", "待定", "详见", "见正文", "面试", "到面", "落户")


def _is_location_noise_value(value: str) -> bool:
    """True when a captured ``地点`` value is page chrome, not a city."""
    if not value:
        return True
    stripped = value.strip()
    if not stripped:
        return True
    if stripped in _LOCATION_NOISE_MARKERS:
        return True
    # A bare numeral count ("2") or a suffix-only value is not a city.
    if stripped.isdigit():
        return True
    return any(stripped.startswith(marker) for marker in ("共", "推测", "约", "待定"))


def _extract_locations(text: str) -> list[str]:
    """Extract location strings from text."""
    locations: list[str] = []
    for pattern in _LOCATION_PATTERNS:
        m = pattern.search(text)
        if m:
            loc_text = m.group(1).strip()
            if _is_location_noise_value(loc_text):
                continue
            # Split on common delimiters
            parts = re.split(r"[,;、/\s]{2,}", loc_text)
            for part in parts:
                part = part.strip()
                if part and not _is_location_noise_value(part) and len(part) <= 50:
                    locations.append(part)
    return locations


def _detect_recruitment_types(text: str) -> list[str]:
    """Detect recruitment type keywords in text."""
    types: list[str] = []
    for pattern, type_name in _RECRUITMENT_TYPE_RULES:
        if pattern.search(text) and type_name not in types:
            types.append(type_name)
    return types


def _extract_apply_method(text: str) -> dict | None:
    """Extract application method information."""
    for pattern in _APPLY_METHOD_PATTERNS:
        m = pattern.search(text)
        if m:
            method_text = m.group(1).strip()
            # Check if there's an email in the apply method
            email_match = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", method_text)
            if email_match:
                return {
                    "method": "email",
                    "email": email_match.group(),
                    "gui_eligible": False,
                }
            return {
                "method": "unknown",
                "gui_eligible": True,
            }
    return None


def _extract_deadline(text: str) -> str | None:
    """Extract deadline text."""
    for pattern in _DEADLINE_PATTERNS:
        m = pattern.search(text)
        if m:
            deadline = m.group(1).strip()
            if 1 <= len(deadline) <= 100:
                return deadline
    return None


def _extract_published_at(text: str) -> str | None:
    """Extract an explicit posting timestamp, never a crawl timestamp."""
    for pattern in _PUBLISHED_AT_PATTERNS:
        match = pattern.search(text)
        if match:
            value = match.group(1).strip()
            if value:
                return value
    return None


def _extract_referral_code(text: str) -> str | None:
    """Extract referral / referral code."""
    for pattern in _REFERRAL_CODE_PATTERNS:
        m = pattern.search(text)
        if m:
            code = m.group(1).strip()
            if code and len(code) <= 40:
                return code
    return None


def _extract_min_degree(text: str) -> str | None:
    """Extract the minimum degree requirement from JD text (whitelist-first).

    Keywords are scanned most-specific-first so ``学历不限`` beats the bare
    ``不限`` and ``博士及以上`` is caught by the ``博士`` keyword.  A JD with
    no degree mention returns None (safe default, never fabricated).
    """
    lowered = text.lower()
    for keyword, normalized in _DEGREE_RULES:
        if keyword in lowered:
            return normalized
    return None


def _extract_priority(text: str) -> str:
    """Classify whether a requirement is must-have or preferred.

    ``must`` wins over ``preferred`` when both appear in one segment; no
    signal returns the safe ``unknown`` default.
    """
    if _PRIORITY_MUST_RE.search(text):
        return "must"
    if _PRIORITY_PREFERRED_RE.search(text):
        return "preferred"
    return "unknown"


def _iguopin_detail_overrides(text: str, url: str) -> dict[str, object]:
    """Extract Iguopin detail fields without inheriting global portal chrome."""
    if not re.match(
        r"https?://(?:www\.)?iguopin\.com/job/detail(?:[?#]|$)", url, re.IGNORECASE
    ):
        return {}

    overrides: dict[str, object] = {}
    title_match = re.search(
        r"(?m)^([^\n]{2,80})\n更新于\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}举报\s*$",
        text,
    )
    if title_match:
        overrides["title"] = title_match.group(1).strip()

    unit_match = re.search(
        r"(?s)(?:^|\n)单位信息\s*\n(.+?)(?:\n\d+\s*个在招职位|\n频道链接|$)",
        text,
    )
    if unit_match:
        for line in unit_match.group(1).splitlines():
            candidate = line.strip()
            if (
                len(candidate) >= 3
                and candidate not in {"关注", "单位信息"}
                and not candidate.endswith("在招职位")
            ):
                overrides["company"] = candidate
                break

    nature_match = re.search(r"(?m)^职位性质\s*[:：]\s*([^\n]+)", text)
    if nature_match:
        overrides["recruitment_types"] = _detect_recruitment_types(
            nature_match.group(1)
        )

    degree_match = re.search(r"(?m)^最低学历\s*[:：]\s*([^\n]+)", text)
    if degree_match:
        overrides["min_degree"] = _extract_min_degree(degree_match.group(1))

    requirements = _extract_section(text, _IGUOPIN_REQUIREMENTS_HEADERS)
    if requirements:
        overrides["requirements"] = requirements
    return overrides


def _smartedu_detail_overrides(text: str, url: str) -> dict[str, object]:
    """Extract one 24365 detail JD without inheriting national-site chrome."""
    if not re.match(
        r"https?://24365\.smartedu\.cn/student/jobs/[^/?#]+/detail\.html(?:[?#]|$)",
        url,
        re.IGNORECASE,
    ):
        return {}

    overrides: dict[str, object] = {}
    title_match = re.search(
        r"(?m)^([^\n]{2,100}(?:实习生|工程师|开发|经理|岗位)\([^\n()]{2,80}\))\s*$",
        text,
    )
    if not title_match:
        title_match = re.search(
            r"(?m)^([^\n]{2,100}(?:实习生|工程师|开发|经理|岗位))\n\[\n(?:兼职|全职)",
            text,
        )
    if title_match:
        title = title_match.group(1).strip()
        overrides["title"] = title
        location_match = re.search(r"\(([^()]+)\)\s*$", title)
        if location_match:
            overrides["locations"] = [
                item.strip()
                for item in re.split(r"[/、,，]", location_match.group(1))
                if item.strip()
            ]
        overrides["recruitment_types"] = _detect_recruitment_types(title)

    company_match = re.search(
        r"(?m)^([^\n]{3,100}(?:有限公司|公司|研究院|大学|学校|中心))\n所属行业\s*$",
        text,
    )
    if company_match:
        overrides["company"] = company_match.group(1).strip()

    sections_match = re.search(
        r"(?s)职位描述\s*[:：]\s*(.*?)\s*职位要求\s*[:：]\s*(.*?)"
        r"(?=\n[^\n]{3,100}(?:有限公司|公司|研究院|大学|学校|中心)\n所属行业(?:\n|$)|$)",
        text,
    )
    if sections_match:
        overrides["responsibilities"] = sections_match.group(1).strip()
        overrides["requirements"] = sections_match.group(2).strip()
    if "职位已下线" in text:
        overrides["closed"] = True
    return overrides


def _estimate_confidence(
    title: str | None,
    company: str | None,
    responsibilities: str,
    requirements: str,
    has_section_content: bool,
) -> float:
    """Estimate how likely this text is a real job posting."""
    score = 0.0
    if title:
        score += 0.25
    if company:
        score += 0.20
    if responsibilities:
        score += 0.25
    if requirements:
        score += 0.20
    if has_section_content:
        score += 0.10
    return min(score, 1.0)


def extract_jd_candidates(page_text: str, url: str) -> list[NormalizedJobCandidate]:
    """Parse job description text using deterministic keyword heuristics.

    This is a pure function — no LLM, no DB, no network.
    Returns 0-100 NormalizedJobCandidate objects with confidence scores.

    For structured pages (with clear section headers like 岗位职责/任职要求),
    extracts precise fields. For unstructured text (WeChat articles, OCR results),
    uses aggressive heuristics to extract whatever information is available.

    Args:
        page_text: Raw text content from a job detail page or article.
        url: The source URL for reference.

    Returns:
        List of extracted candidates (0-10, one per distinct position).
    """
    page_text = page_text or ""

    if not page_text.strip():
        return []

    # Split multi-job pages into individual segments
    segments = _split_multi_job_page(page_text)

    results: list[NormalizedJobCandidate] = []
    seen_dedup_keys: set[str] = set()

    for segment in segments:
        # Hard ceiling per page: Feishu card listings segment into dozens of
        # openings, so this guard is reachable and covered by tests.
        if len(results) >= _MAX_CANDIDATES_PER_PAGE:
            break

        title, title_conf = _extract_title(segment)
        company, company_conf = _extract_company(segment)
        department = _extract_department(segment)
        responsibilities = _extract_section(segment, _RESPONSIBILITIES_HEADERS)
        requirements = _extract_section(segment, _REQUIREMENTS_HEADERS)
        locations = _extract_locations(segment)
        recruitment_types = _detect_recruitment_types(segment)
        apply_method = _extract_apply_method(segment)
        deadline = _extract_deadline(segment)
        published_at = _extract_published_at(segment)
        referral_code = _extract_referral_code(segment)
        min_degree = _extract_min_degree(segment)
        priority = _extract_priority(segment)

        iguopin_overrides = _iguopin_detail_overrides(segment, url)
        smartedu_overrides = _smartedu_detail_overrides(segment, url)
        title = iguopin_overrides.get("title", title)
        company = iguopin_overrides.get("company", company)
        recruitment_types = iguopin_overrides.get(
            "recruitment_types", recruitment_types
        )
        min_degree = iguopin_overrides.get("min_degree", min_degree)
        requirements = iguopin_overrides.get("requirements", requirements)

        title = smartedu_overrides.get("title", title)
        company = smartedu_overrides.get("company", company)
        locations = smartedu_overrides.get("locations", locations)
        recruitment_types = smartedu_overrides.get(
            "recruitment_types", recruitment_types
        )
        responsibilities = smartedu_overrides.get(
            "responsibilities", responsibilities
        )
        requirements = smartedu_overrides.get("requirements", requirements)

        has_section_content = bool(responsibilities or requirements)

        # ── Fallback for unstructured text ──
        # When no structured sections are found (common in WeChat articles,
        # OCR text), treat the full segment as a description and extract
        # whatever we can from keywords and context.
        uses_unstructured_fallback = False
        if not responsibilities and not requirements and not title:
            # Try harder: look for position-related keywords anywhere in text
            fm_title = _fuzzy_extract_title(segment)
            if fm_title:
                title = fm_title
            uses_unstructured_fallback = True

        # Build warnings
        warnings: list[str] = []
        if not title:
            warnings.append("No job title found via heuristics")
        if not responsibilities and not requirements:
            warnings.append("No responsibilities or requirements sections found")
        if not locations:
            warnings.append("No location information found")
        if smartedu_overrides.get("closed"):
            warnings.append("Source page explicitly states 职位已下线")

        # ── Build description_text ──
        desc_parts = []
        if responsibilities:
            desc_parts.append(responsibilities)
        if requirements:
            desc_parts.append(requirements)
        if desc_parts:
            description_text = "\n\n".join(desc_parts)
        elif uses_unstructured_fallback and len(segment.strip()) >= 20:
            # For unstructured text, use the full segment as description
            # (trimmed to a reasonable length)
            description_text = segment.strip()[:4000]
        else:
            description_text = segment[:2000]

        # ── Deduplicate by title + company ──
        dedup_key = f"{title or ''}|{company or ''}"
        if dedup_key in seen_dedup_keys:
            continue
        seen_dedup_keys.add(dedup_key)

        # ── Adjusted confidence for unstructured text ──
        confidence = _estimate_confidence(
            title, company, responsibilities, requirements, has_section_content
        )
        if uses_unstructured_fallback:
            # Reduce confidence but keep above "too low to use"
            confidence = max(confidence, 0.35)

        candidate = NormalizedJobCandidate(
            title=title,
            company_name=company,
            department=department,
            description_text=description_text,
            responsibilities=responsibilities,
            requirements=requirements,
            locations=locations,
            recruitment_types=recruitment_types,
            apply_url=url,
            application_channel_json=apply_method,
            deadline_text=deadline,
            published_at=published_at,
            referral_code=referral_code,
            confidence=confidence,
            normalization_warnings=warnings,
            min_degree=min_degree,
            priority=priority,
            # B1: strength of the section text (responsibilities + requirements),
            # serialized as a dict; optional input for downstream scoring.
            strength=analyze_job_strength(description_text).to_dict(),
            # B2: deterministic taxonomy [level1, level2], [] when unclassified.
            # Known detail-page chrome contains unrelated portal/category
            # text. Its explicit title is the authoritative role signal;
            # ordinary sources retain the richer title + section classifier.
            taxonomy=taxonomy_tags(
                str(title or "")
                if iguopin_overrides or smartedu_overrides
                else f"{title or ''}\n{description_text}"
            ),
        )

        results.append(candidate)

    # ── If still no results, try whole-text extraction ──
    # Unreachable: the loop above always appends at least one candidate for
    # non-empty text (the first segment is never a dedup hit, and
    # _split_multi_job_page returns >=1 segment for non-empty input). The
    # empty-text case returns early at line ~284. Retained as a last-resort
    # guard; _extract_from_unstructured_text is exercised directly in tests.
    if not results:  # pragma: no cover
        result = _extract_from_unstructured_text(page_text, url)
        if result:
            results.append(result)

    return results


def _fuzzy_extract_title(text: str) -> str | None:
    """Try to find a job title in unstructured text using keyword proximity.

    Looks for patterns like:
    - "招募XXX岗位" / "招聘XXX" / "招收XXX(实习)"
    - "岗位包括：XXX、YYY" / "职位：XXX"
    - "面向XXX专业招聘XXX"
    """
    # Pattern 1: 招募/招聘/招收 + job-like noun phrase (or just the recruitment prefix)
    m = re.search(
        r"(?:招募|招聘|招收|急招)[：:\s]*"
        r"(.{2,60}?(?:工程师|经理|专员|设计师|分析师|运营|开发|"
        r"实习生|实习|培训生|管培生|顾问|助理|主管|总监|代表|"
        r"岗位|职位|人员|人才))",
        text,
    )
    if m:
        return m.group(1).strip()[:60]

    # Pattern 1b: Company丨Recruitment Title (WeChat article title format)
    m = re.search(r"(.{2,40})[丨\|\-]\s*(.{2,60}?(?:招聘|实习|校招|招募|内推|春招|秋招).{0,30})", text)
    if m:
        # Return the part after 丨 as the title (more likely the job description)
        return m.group(2).strip()[:60]

    # Pattern 2: title-like line starting with position keywords
    m = re.search(
        r"(?:岗位|职位|招聘岗位|招聘职位)[：:\s]*"
        r"(.{2,60})",
        text,
    )
    if m:
        candidate = m.group(1).strip()
        # Filter out lines that are clearly not job titles
        if len(candidate) <= 60 and not candidate.startswith("http"):
            return candidate

    # Pattern 3: line ending with 岗 or 岗位
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"(.{2,30}(?:岗|岗位))$", line)
        if m:
            title_text = m.group(1)
            if not any(skip in title_text for skip in ("投递", "招聘", "求职", "关注")):
                return title_text

    # Pattern 4: 丨 separated title (common in WeChat article titles)
    # Use 2+ chars prefix to capture "XX招聘丨YY" style titles
    m = re.search(r"(.{2,60}?(?:招聘|实习|校招|内推|招募|春招|秋招).{0,30})", text)
    if m:
        raw = m.group(1).strip()
        # Clean up: strip leading/trailing separators and repeated content
        raw = re.sub(r"^[丨|\-\s]+", "", raw)
        raw = re.sub(r"[丨|\-\s]+$", "", raw)
        if len(raw) <= 80:
            return raw[:60]

    # Pattern 5: First meaningful line looks like a title
    first_line = text.strip().split("\n")[0].strip() if text.strip() else ""
    if first_line and len(first_line) >= 4 and len(first_line) <= 100:
        # Check if it contains recruitment-related keywords
        rec_kw = ["招聘", "实习", "校招", "招募", "内推", "春招", "秋招", "岗位", "入职"]
        if any(kw in first_line for kw in rec_kw):
            # Clean up common prefixes
            clean = re.sub(r"^(?:原创|分享|收藏|点赞|在看)\s*", "", first_line)
            clean = re.sub(r"\s*!{1,3}$", "", clean)
            return clean[:60]

    return None


def _extract_from_unstructured_text(text: str, url: str) -> NormalizedJobCandidate | None:
    """Last-resort extraction from completely unstructured text.

    Used when structured section headers and fuzzy title extraction both fail.
    Treats the entire text as a job description and tries to extract at minimum
    a plausible title and recruitment type.
    """
    text = text.strip()
    if len(text) < 50:
        return None

    # Must have at least some recruitment-related keywords
    recruitment_keywords = ["招聘", "实习", "校招", "内推", "岗位", "投递", "简历", "面试",
                            "intern", "campus", "recruit", "job", "career"]
    keyword_hits = sum(1 for kw in recruitment_keywords if kw.lower() in text.lower())
    if keyword_hits < 2:
        return None

    # Try fuzzy title extraction
    title = _fuzzy_extract_title(text)

    # Try to find company name
    company = None
    for pattern in _COMPANY_PATTERNS:
        m = pattern.search(text)
        if m:
            company = m.group(1).strip()
            break

    # If still no company, check first line for company-like content
    if not company:
        first_line = text.split("\n")[0].strip()
        if "丨" in first_line:
            parts = first_line.split("丨")
            # ``split`` on a string known to contain ``丨`` always yields a
            # non-empty list, so the falsy arm here is unreachable.
            if parts:  # pragma: no cover
                company = parts[0].strip()[:60]

    recruitment_types = _detect_recruitment_types(text)
    locations = _extract_locations(text)
    deadline = _extract_deadline(text)
    published_at = _extract_published_at(text)
    referral_code = _extract_referral_code(text)

    return NormalizedJobCandidate(
        title=title or "招聘信息",
        company_name=company,
        description_text=text[:4000],
        locations=locations,
        recruitment_types=recruitment_types,
        apply_url=url,
        deadline_text=deadline,
        published_at=published_at,
        referral_code=referral_code,
        confidence=0.30,
        min_degree=_extract_min_degree(text),
        priority=_extract_priority(text),
        strength=analyze_job_strength(text[:4000]).to_dict(),
        taxonomy=taxonomy_tags(f"{title or ''}\n{text[:4000]}"),
        normalization_warnings=[
            "Unstructured text extraction — fields may be incomplete",
            "No structured sections found; full text used as description",
        ],
    )
