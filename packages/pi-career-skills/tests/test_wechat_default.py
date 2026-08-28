from pi_career_skills.network import wechat


def test_wechat_ocr_is_enabled_by_default() -> None:
    assert wechat._WECHAT_OCR_ENABLED is True
