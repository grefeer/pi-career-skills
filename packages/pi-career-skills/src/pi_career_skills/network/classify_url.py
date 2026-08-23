"""URL site-classification tool for the ``job-discovery`` Skill (P2-5).

Verbatim port of ``skill/job_discovery/runtime/classify_url.py``.  A
deterministic, low-budget classifier that tells the Executor how to approach a
candidate URL *before* it spends a fetch/playwright budget.  Host-level
signals need no network; everything else is judged from a 4KB HTML probe (no
browser, no full render).  Site classes mirror the skill's site-catalog
classification:

  - ``wechat``   -- mp.weixin.qq.com / ReadGZH mirror (OCR pipeline);
  - ``adapter``  -- host covered by a certified A1 adapter (fetch via JSON API);
  - ``static``   -- probe HTML already carries JD text / visible content;
  - ``spa``      -- probe is a JS shell (Playwright render required);
  - ``blocked``  -- probe failed, non-200, or anti-bot markers.

Pi adaptations: the input/output models live in
``..business.job_discovery.models`` (byte-identical per the §6 contract
snapshot gate); ``PublicFetchError`` (``.url_guard``) replaces the source's
``PublicJobFetchError``; ``_fetch_validated`` / ``_adapter_company_for_url``
come from ``.page_fetch`` / ``.adapters``.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..business.job_discovery.models import (
    ClassifiedJobUrl,
    ClassifyJobUrlInput,
    ClassifyJobUrlOutput,
)
from ..context import ToolContext
from .adapters import _adapter_company_for_url
from .page_fetch import _fetch_validated
from .url_guard import PublicFetchError

#: Host-level signals, evaluated without any network request.
_WECHAT_HOSTS = frozenset({"mp.weixin.qq.com", "readgzh.com"})

#: HTML head markers that indicate an anti-bot gate rather than a page.
_ANTI_BOT_MARKERS = (
    "captcha",
    "验证码",
    "滑块验证",
    "安全验证",
    "access denied",
    "访问被拒绝",
)

#: JD section markers whose presence means the page text is extractable now.
_JD_SECTION_MARKERS = (
    "岗位职责",
    "职位描述",
    "任职要求",
    "岗位要求",
    "职位要求",
    "工作地点",
)

#: Probe size in bytes -- enough to see markers and text, far from a full page.
_PROBE_BYTES = 4096
_MIN_VISIBLE_TEXT_CHARS = 200


def classify_job_url(
    context: ToolContext, payload: ClassifyJobUrlInput
) -> ClassifyJobUrlOutput:
    """Classify each candidate URL from host/path signals and a 4KB probe."""
    return ClassifyJobUrlOutput(
        results=[_classify_one(url) for url in payload.urls]
    )


def _classify_one(url: str) -> ClassifiedJobUrl:
    """One URL through the signal cascade: host -> adapter -> probe."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host in _WECHAT_HOSTS:
        return ClassifiedJobUrl(
            url=url, site_class="wechat", evidence_signal=f"host={host}"
        )
    company = _adapter_company_for_url(url)
    if company is not None:
        return ClassifiedJobUrl(
            url=url, site_class="adapter", evidence_signal=f"adapter={company}"
        )
    try:
        fetched = _fetch_validated(url)
        response = getattr(fetched, "response", fetched)
        effective_url = getattr(fetched, "effective_url", url)
    except PublicFetchError as exc:
        return ClassifiedJobUrl(
            url=url, site_class="blocked", evidence_signal=str(exc)
        )
    head = response.content[:_PROBE_BYTES].decode("utf-8", errors="replace").lower()
    effective_host = (urlsplit(effective_url).hostname or "").lower()
    effective_path = urlsplit(effective_url).path.lower()
    if (
        effective_host == "safe.liepin.com"
        and ("captcha" in effective_path or "security" in effective_path)
    ):
        return ClassifiedJobUrl(
            url=url, site_class="blocked", evidence_signal="anti_bot_challenge"
        )
    if response.status_code != 200:
        return ClassifiedJobUrl(
            url=url,
            site_class="blocked",
            evidence_signal=f"http_{response.status_code}",
        )
    if any(marker in head for marker in _ANTI_BOT_MARKERS):
        return ClassifiedJobUrl(
            url=url, site_class="blocked", evidence_signal="anti_bot_markers"
        )
    if any(marker in head for marker in _JD_SECTION_MARKERS):
        return ClassifiedJobUrl(
            url=url, site_class="static", evidence_signal="jd_section_markers"
        )
    if _visible_text_length(head) >= _MIN_VISIBLE_TEXT_CHARS:
        return ClassifiedJobUrl(
            url=url, site_class="static", evidence_signal="visible_text"
        )
    return ClassifiedJobUrl(url=url, site_class="spa", evidence_signal="js_bundle_only")


def _visible_text_length(html: str) -> int:
    """Rough visible-text length: scripts/styles and tags do not count."""
    stripped = re.sub(
        r"<(?:script|style)[^>]*>.*?</(?:script|style)>", " ", html, flags=re.DOTALL | re.IGNORECASE
    )
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    return len(re.sub(r"\s+", "", stripped))


__all__ = [
    "_WECHAT_HOSTS",
    "_ANTI_BOT_MARKERS",
    "_JD_SECTION_MARKERS",
    "_PROBE_BYTES",
    "_MIN_VISIBLE_TEXT_CHARS",
    "classify_job_url",
    "_classify_one",
    "_visible_text_length",
]
