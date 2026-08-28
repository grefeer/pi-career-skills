from __future__ import annotations

from pi_career_skills.network.subprocess_runner import SKILL_DIR, run_skill_script


def test_package_local_ocr_script_is_bundled() -> None:
    assert (SKILL_DIR / "ocr_image.py").is_file()
    result = run_skill_script("ocr_image", argv=["missing-image.png"])
    assert '"code": "image_not_found"' in result


def test_subprocess_runner_does_not_expose_legacy_scripts() -> None:
    assert "script not allowed" in run_skill_script("browse", ["https://example.com"])
