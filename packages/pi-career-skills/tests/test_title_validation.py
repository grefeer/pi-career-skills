"""Tests for business/job_discovery/title_validation.py.

The three helpers are a verbatim port of ``_extract_portal_role_text`` /
``_infer_official_page_title`` / ``_is_plausible_job_title`` from the source
project's ``skill/job_discovery/runtime/job_discovery.py``; these tests pin
their observable behavior (portal-role extraction, header frequency voting,
chrome / numbered-safety-note rejection).
"""

from __future__ import annotations

from pi_career_skills.business.job_discovery.title_validation import (
    _CAMPUS_PORTAL_HOST,
    _extract_portal_role_text,
    _infer_official_page_title,
    _is_plausible_job_title,
)


class TestExtractPortalRoleText:
    def test_campus_portal_host_constant(self):
        assert _CAMPUS_PORTAL_HOST == "career.hebut.edu.cn"

    def test_non_portal_url_returns_empty(self):
        assert _extract_portal_role_text(
            "职位类型：后端开发工程师", "https://example.com/jobs/1"
        ) == ""

    def test_portal_host_wrong_path_returns_empty(self):
        assert _extract_portal_role_text(
            "职位类型：后端开发工程师",
            "https://career.hebut.edu.cn/other/123",
        ) == ""

    def test_hebut_portal_content_url_extracts_role_text(self):
        text = (
            "职位类型：后端开发工程师（实习）\n"
            "招聘流程：简历筛选\n"
            "工作地点：天津"
        )
        result = _extract_portal_role_text(
            text, "https://career.hebut.edu.cn/correcruit/content/123.html"
        )
        assert result == "后端开发工程师（实习）"

    def test_hebut_portal_no_role_line_returns_empty(self):
        assert _extract_portal_role_text(
            "招聘流程：简历筛选", "https://career.hebut.edu.cn/correcruit/content/1"
        ) == ""


class TestInferOfficialPageTitle:
    def test_repeated_real_title_wins_over_one_off_nav_label(self):
        text = (
            "后端开发工程师\n"
            "后端开发工程师\n"
            "浏览职位\n"
            "岗位职责：负责后端服务设计与开发"
        )
        assert _infer_official_page_title(text) == "后端开发工程师"

    def test_long_body_sentence_rejected(self):
        text = (
            "后端开发工程师\n" * 3
            + "这里是一句非常非常长的句子，超过四十个字符，根本不可能作为职位标题使用，只会是正文\n"
            + "岗位职责：负责后端服务设计与开发"
        )
        assert _infer_official_page_title(text) == "后端开发工程师"

    def test_only_nav_labels_returns_none(self):
        text = "浏览职位\n岗位职责：负责后端服务设计与开发"
        assert _infer_official_page_title(text) is None

    def test_empty_text_returns_none(self):
        assert _infer_official_page_title("") is None


class TestIsPlausibleJobTitle:
    def test_rejects_nav_chrome_labels(self):
        for label in ("查看全部", "职位信息", "浏览职位", "申请职位"):
            assert not _is_plausible_job_title(label), f"{label} should be rejected"

    def test_rejects_numbered_safety_notes(self):
        assert not _is_plausible_job_title("1. 请勿相信任何收费信息")
        assert not _is_plausible_job_title("2、如您应聘请注意安全")
        assert not _is_plausible_job_title("温馨提示：谨防诈骗")

    def test_rejects_too_short_or_empty(self):
        assert not _is_plausible_job_title("")
        assert not _is_plausible_job_title("A")
        assert not _is_plausible_job_title(None)
        assert not _is_plausible_job_title(123)

    def test_accepts_real_titles(self):
        assert _is_plausible_job_title("后端工程师")
        assert _is_plausible_job_title("Senior Software Engineer")
        assert _is_plausible_job_title("算法工程师（北京）")
