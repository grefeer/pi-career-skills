"""WeChat image-article (OCR) tool for the ``job-discovery`` Skill.

Verbatim port of ``skill/job_discovery/runtime/wechat.py`` (the OCR gate and
``fetch_wechat_article``) plus ``_fetch_wechat_article_page`` from
``skill/job_discovery/runtime/job_discovery.py`` (1727-1760) and
``_WECHAT_ARTICLE_HOST`` (325).  The channel is gated by
``Settings.job_discovery_ocr_enabled`` (mirror of the Playwright fallback
toggle): when off, the tool reports ``needs_manual_review`` (reason
``ocr_disabled``) without touching the network.

Pi adaptations (behavior identical): the input/output models live in
``..business.job_discovery.models`` (byte-identical per the §6 contract
snapshot gate); ``_WECHAT_OUT_DIR_DEFAULT`` drops the source tree's
``backend/`` segment (``parents[3] / "var" / "job-discovery-skill" / "ocr"``).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from ..business.job_discovery.handlers import extract_observed_job_details
from ..business.job_discovery.models import (
    FetchPublicJobPageOutput,
    FetchWechatArticleInput,
    FetchWechatArticleOutput,
)
from ..context import ToolContext
from .url_guard import PublicFetchError
from .wechat_slice import run_wechat_slice

#: The WeChat OCR channel owns ``mp.weixin.qq.com`` article pages the same way
#: a certified adapter owns its hosts: their bodies are image content that the
#: plain requests/render chain reads as an empty shell, so the fetch tool
#: routes them to the OCR slice (``wechat.fetch_wechat_article``) before the
#: generic chain.
_WECHAT_ARTICLE_HOST = "mp.weixin.qq.com"

_WECHAT_OCR_ENABLED = False
_WECHAT_OUT_DIR_DEFAULT = str(
    Path(__file__).resolve().parents[3] / "var" / "job-discovery-skill" / "ocr"
)


def enable_wechat_ocr(enabled: bool) -> None:
    """Toggle the WeChat OCR channel (called from runtime assembly)."""
    global _WECHAT_OCR_ENABLED
    _WECHAT_OCR_ENABLED = enabled


def fetch_wechat_article(
    context: ToolContext, payload: FetchWechatArticleInput
) -> FetchWechatArticleOutput:
    """OCR one WeChat image article into text + candidates (Levels 1-6).

    When the OCR channel is disabled the tool returns
    ``needs_manual_review`` (``ocr_disabled``) without any network access;
    the runtime then surfaces the human question instead of a hard failure.
    """
    if not _WECHAT_OCR_ENABLED:
        return FetchWechatArticleOutput(
            url=payload.url,
            status="needs_manual_review",
            channel=None,
            candidates=[],
            ocr_text="",
            needs_deep_crawl=False,
            reason="ocr_disabled",
        )
    result = run_wechat_slice(
        payload.url,
        out_dir=payload.out_dir or _WECHAT_OUT_DIR_DEFAULT,
        context=context,
        extract_fn=extract_observed_job_details,
    )
    content_hash = result.content_hash
    return FetchWechatArticleOutput(
        url=result.url,
        status=result.status,
        channel=result.channel,
        candidates=result.candidates,
        ocr_text=result.visible_text,
        needs_deep_crawl=result.needs_deep_crawl,
        reason=result.reason,
        artifact_id=f"observed:{content_hash}" if content_hash else None,
        source_url=result.url,
        content_hash=content_hash,
        visible_text=result.visible_text,
    )


def _fetch_wechat_article_page(
    context: ToolContext, url: str
) -> FetchPublicJobPageOutput | None:
    """OCR-route a WeChat article URL; None when the host is not WeChat.

    Mirrors the adapter-first contract for the WeChat channel: the OCR slice
    is authoritative for ``mp.weixin.qq.com`` pages, so a gated-off channel
    is a hard ``wechat_ocr_disabled`` error, never a silent fallthrough to
    the empty-page path. A slice run that produced no text is
    ``wechat_ocr_failed``; usable text becomes the same memory-bound page
    evidence shape as browsed pages (sha256 content hash, ``observed:`` id).
    """
    host = (urlsplit(url).hostname or "").lower()
    if host != _WECHAT_ARTICLE_HOST:
        return None
    result = fetch_wechat_article(context, FetchWechatArticleInput(url=url))
    if result.status == "needs_manual_review" and result.reason == "ocr_disabled":
        raise PublicFetchError("wechat_ocr_disabled")
    if not result.content_hash or not result.visible_text:
        raise PublicFetchError(
            "wechat_ocr_failed",
            message="该微信链接抓取失败（镜像验证墙/付费墙或无正文）仅代表此 URL 本身不可用，不代表同批其他微信链接也会失败；同批其余 URL 仍应继续逐一尝试。",
        )
    return FetchPublicJobPageOutput(
        artifact_id=result.artifact_id,
        source_url=url,
        title=None,
        visible_text=result.visible_text,
        content_hash=result.content_hash,
    )


__all__ = [
    "_WECHAT_ARTICLE_HOST",
    "_WECHAT_OCR_ENABLED",
    "_WECHAT_OUT_DIR_DEFAULT",
    "enable_wechat_ocr",
    "fetch_wechat_article",
    "_fetch_wechat_article_page",
]
