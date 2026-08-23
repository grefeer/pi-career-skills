"""Golden-parity tests for extract_jd_candidates against source-project fixtures.

Each test runs the ported extractor on the same input used to generate the
source-side fixture and asserts canonical (sorted-key) JSON equality.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from pi_career_skills.business.job_discovery.jd_extraction import extract_jd_candidates

FIXTURES = Path(__file__).parent / "fixtures"

DETAIL_PAGE = """前端开发工程师
公司名称：深圳云启科技
所属部门：前端研发部
岗位职责
1. 负责公司核心产品的前端开发，使用 Vue3、TypeScript、Vite
2. 参与组件库建设与性能优化
3. 与后端协作完成接口联调
任职要求
1. 本科及以上学历，计算机相关专业
2. 2 年以上前端开发经验，熟悉 Vue3、TypeScript、Vite
3. 熟悉 Git、Webpack 构建
薪资：15-25K·14薪
工作地点：深圳·南山
招聘类型：社会招聘
发布时间：2026-08-20
投递方式：apply@yunqi.example.com
"""

LIST_PAGE = """深圳云启科技招聘
1. 前端开发工程师
   职责：负责 Vue3 项目开发；要求：2 年经验
   地点：深圳
2. 后端开发工程师
   职责：负责 Java 服务端开发；要求：3 年经验
   地点：深圳
3. 算法工程师
   职责：负责推荐算法；要求：硕士优先
   地点：北京
"""

NOISY_PAGE = """云启科技成立于 2018 年，是一家专注于企业级 SaaS 的公司。我们正在招聘
开发工程师加入我们，负责公司产品的研发工作。公司福利包括五险一金、
弹性工作、年度体检等。如有兴趣请联系 hr@yunqi.example.com。
"""


def _jsonable(payload: object) -> object:
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        return _jsonable(dataclasses.asdict(payload))
    if isinstance(payload, list):
        return [_jsonable(x) for x in payload]
    if isinstance(payload, dict):
        return {k: _jsonable(v) for k, v in payload.items()}
    return payload


def _canonical(payload: object) -> str:
    return json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, indent=1)


def test_extract_single_jd_parity() -> None:
    """Single detail-page extraction must match the source golden fixture."""
    result = extract_jd_candidates(
        DETAIL_PAGE, "https://yunqi.example.com/jobs/frontend"
    )
    expected = json.loads((FIXTURES / "extract_single_jd.json").read_text(encoding="utf-8"))
    assert _canonical(result) == _canonical(expected)


def test_extract_list_page_parity() -> None:
    """List-page extraction must match the source golden fixture."""
    result = extract_jd_candidates(
        LIST_PAGE, "https://yunqi.example.com/jobs"
    )
    expected = json.loads((FIXTURES / "extract_list_page.json").read_text(encoding="utf-8"))
    assert _canonical(result) == _canonical(expected)


def test_extract_unstructured_parity() -> None:
    """Unstructured/noisy page extraction must match the source golden fixture."""
    result = extract_jd_candidates(
        NOISY_PAGE, "https://yunqi.example.com/about"
    )
    expected = json.loads((FIXTURES / "extract_unstructured.json").read_text(encoding="utf-8"))
    assert _canonical(result) == _canonical(expected)
