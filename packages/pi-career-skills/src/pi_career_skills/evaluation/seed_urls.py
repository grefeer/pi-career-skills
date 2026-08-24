"""Per-question seed URL map — source-parity seeds plus local skill support.

The SEED_URLS mapping is copied byte-for-byte from
``tests/question/parity_support.py`` in the source project to guarantee
evaluation parity.  ``ALL_SKILLS`` intentionally includes the migrated
``career-planning`` capability even though it requires no seed URLs.
Do NOT "improve" the URLs or notes.
"""

from __future__ import annotations

ALL_SKILLS: list[str] = [
    "job-discovery",
    "job-matching",
    "resume-tailoring",
    "career-planning",
]

_LIEPIN_ROLE_URLS = {
    "frontend": "https://www.liepin.com/zpqiandongruanjiankaifagongchengshi/",
    "llm-dev": "https://www.liepin.com/zpdmxyykfgcsz24g/",
}
_IGUOPIN_SEARCH = {
    "java": "https://www.iguopin.com/job/list?keyword=Java%E5%90%8E%E7%AB%AF%E5%BC%80%E5%8F%91%E5%B7%A5%E7%A8%8B%E5%B8%88",
    "frontend": "https://www.iguopin.com/job/list?keyword=%E5%89%8D%E7%AB%AF%E5%BC%80%E5%8F%91%E5%B7%A5%E7%A8%8B%E5%B8%88",
    "llm": "https://www.iguopin.com/job/list?keyword=%E5%A4%A7%E6%A8%A1%E5%9E%8B%E5%BA%94%E7%94%A8%E5%BC%80%E5%8F%91",
    "ai": "https://www.iguopin.com/job/list?keyword=AI%E7%AE%97%E6%B3%95%E5%B7%A5%E7%A8%8B%E5%B8%88",
    "pm": "https://www.iguopin.com/job/list?keyword=%E4%BA%A7%E5%93%81%E7%BB%8F%E7%90%86",
}

_LIEPIN_ROLE_URLS.update(
    {
        "java": "https://www.liepin.com/zphouduanjavakaifagongchengshi/",
        "aigc": "https://www.liepin.com/zpchanpinjingli/",
    }
)

_IGUOPIN_AIGC_PM = "https://www.iguopin.com/job/list?keyword=AIGC%E4%BA%A7%E5%93%81%E7%BB%8F%E7%90%86"
_IGUOPIN_AI_DEV = "https://www.iguopin.com/job/list?keyword=AI%E5%BA%94%E7%94%A8%E5%BC%80%E5%8F%91"

_BAIDU_TALENT_URLS = [
    "https://talent.baidu.com/jobs/detail/GRADUATE/4f1cbc80-8332-4a92-b8fa-c0132b17d47e",
    "https://talent.baidu.com/jobs/detail/GRADUATE/74d83772-1bd0-42b9-8cc5-69eb45696b62",
    "https://talent.baidu.com/jobs/detail/SOCIAL/75d3af47-7f79-4d71-862b-6fbca577bb19",
    "https://talent.baidu.com/jobs/detail/GRADUATE/3287bb6a-8c27-4648-a3c2-b3cac16c3d36",
    "https://talent.baidu.com/jobs/detail/GRADUATE/6f9c3a86-6557-409d-8fa7-e6f4c68d6765",
    "https://talent.baidu.com/jobs/detail/SOCIAL/5bb42582-10ab-4f49-94a6-7ee296885d8f",
    "https://talent.baidu.com/jobs/detail/INTERN/cd423c1c-7a35-4672-b0a7-2857308efe43",
]
_CAMPUS_EVIDENCE = [
    "https://career.hebut.edu.cn/correcruit/content/id/78016.html",
    "https://job.ncss.cn/student/m/index.html",
]

SEED_URLS: dict[str, tuple[list[str], str]] = {
    "Q011": (
        [_LIEPIN_ROLE_URLS["frontend"], _IGUOPIN_SEARCH["frontend"]],
        "liepin frontend landing + iguopin frontend search fallback",
    ),
    "Q013": (
        [_LIEPIN_ROLE_URLS["llm-dev"], _IGUOPIN_SEARCH["llm"]],
        "liepin LLM-dev landing + iguopin large-model application fallback",
    ),
    "Q017": (
        [*_BAIDU_TALENT_URLS, _CAMPUS_EVIDENCE[0]],
        "Baidu graduate JD + campus evidence",
    ),
    "Q028": (_CAMPUS_EVIDENCE, "campus job pages"),
    "Q034": ([_IGUOPIN_SEARCH["java"]], "iguopin Java backend search"),
    "Q040": (
        [_LIEPIN_ROLE_URLS["aigc"], _IGUOPIN_AIGC_PM],
        "Liepin AIGC product-manager landing + iguopin AIGC PM fallback",
    ),
    "Q045": (
        [_LIEPIN_ROLE_URLS["llm-dev"]],
        "liepin large-model application development landing page",
    ),
    "Q055": (
        [_LIEPIN_ROLE_URLS["frontend"], _IGUOPIN_SEARCH["frontend"]],
        "Liepin frontend landing + iguopin search",
    ),
    "Q057": (
        [_LIEPIN_ROLE_URLS["frontend"], _IGUOPIN_SEARCH["frontend"]],
        "Liepin frontend landing + iguopin search",
    ),
    "Q081": (
        [_IGUOPIN_AI_DEV],
        "iguopin AI application development search (ByteDance official not directly reachable)",
    ),
    "Q113": (
        [_IGUOPIN_AI_DEV],
        "iguopin AI application development search for interview-prep JD basis",
    ),
    "Q114": ([_IGUOPIN_SEARCH["java"]], "iguopin Java backend search"),
    "Q115": (
        [_LIEPIN_ROLE_URLS["frontend"], _IGUOPIN_SEARCH["frontend"]],
        "Liepin frontend landing + iguopin search",
    ),
    "Q133": (
        [_IGUOPIN_AI_DEV],
        "iguopin AI application development search (campus-adjacent)",
    ),
    "Q134": (
        [_LIEPIN_ROLE_URLS["java"], _IGUOPIN_SEARCH["java"]],
        "Liepin Java backend landing + iguopin Java fallback",
    ),
    "Q143": (
        [_IGUOPIN_SEARCH["frontend"]],
        "iguopin frontend search (juejin community API gated; public source fallback)",
    ),
    "Q148": (
        [_LIEPIN_ROLE_URLS["aigc"], _IGUOPIN_AIGC_PM],
        "Liepin AIGC product-manager landing + iguopin AIGC PM fallback",
    ),
    # chains: link 1 source seeds; links 2/3 inherit artifacts (no seeds).
    "C001-L1": ([_LIEPIN_ROLE_URLS["llm-dev"]], "liepin 大模型应用开发 role landing page"),
    "C002-L1": ([*_BAIDU_TALENT_URLS, _IGUOPIN_SEARCH["ai"], *_CAMPUS_EVIDENCE], "baidu talent + iguopin AI 算法 + campus fallbacks"),
    "C003-L1": ([_LIEPIN_ROLE_URLS["llm-dev"]], "liepin 大模型应用开发 role landing page"),
    "C004-L1": ([_LIEPIN_ROLE_URLS["llm-dev"]], "liepin 大模型应用开发 role landing page"),
    "C006-L1": ([_LIEPIN_ROLE_URLS["java"], _IGUOPIN_SEARCH["java"]], "liepin 后端 role landing + iguopin Java 搜索 fallback"),
    "C007-L1": ([_LIEPIN_ROLE_URLS["java"], _IGUOPIN_SEARCH["java"]], "liepin 后端 role landing + iguopin Java 搜索 fallback"),
    "C008-L1": ([_LIEPIN_ROLE_URLS["java"], _IGUOPIN_SEARCH["java"]], "liepin 后端 role landing + iguopin Java 搜索 fallback"),
    "C009-L1": ([_LIEPIN_ROLE_URLS["frontend"], _IGUOPIN_SEARCH["frontend"]], "liepin 前端 role landing + iguopin 前端搜索 fallback"),
    "C010-L1": ([_LIEPIN_ROLE_URLS["frontend"], _IGUOPIN_SEARCH["frontend"]], "liepin 前端 role landing + iguopin 前端搜索 fallback"),
    "C011-L1": ([_LIEPIN_ROLE_URLS["frontend"], _IGUOPIN_SEARCH["frontend"]], "liepin 前端 role landing + iguopin 前端搜索 fallback"),
    "C012-L1": ([_LIEPIN_ROLE_URLS["aigc"]], "liepin 产品经理专区 incl. AIGC 专场"),
    "C013-L1": ([_LIEPIN_ROLE_URLS["aigc"]], "liepin 产品经理专区 incl. AIGC 专场"),
    "C014-L1": ([_LIEPIN_ROLE_URLS["aigc"]], "liepin 产品经理专区 incl. AIGC 专场"),
    "C015-L1": ([*_BAIDU_TALENT_URLS, _IGUOPIN_SEARCH["ai"], *_CAMPUS_EVIDENCE], "baidu talent + iguopin AI 算法 + campus fallbacks"),
    # C005-L1 (台账 3 天): no seeds -- smartsheet first, or search/degrade under test.
}


def resolve_seed_urls(qid: str) -> tuple[list[str], str]:
    """Return the seed URLs and note for a question or chain-link id.

    Args:
        qid: Question id (e.g. ``"Q011"``) or chain link id
            (e.g. ``"C001-L1"``).

    Returns:
        Tuple of ``(urls, note)``.  Returns ``([], "no seeds")`` when the
        id is not in the map.
    """
    return SEED_URLS.get(qid, ([], "no seeds"))


__all__ = [
    "ALL_SKILLS",
    "SEED_URLS",
    "resolve_seed_urls",
]
