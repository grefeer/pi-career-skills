"""Copy, normalize, deduplicate, and sanity-check the career evaluation questions."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(
    r"D:\Program Files\JetBrains\PyCharm Community Edition 2024.2.2\proj\langgraph-multi-agent-career-assistant-main\tests\question\redesign"
)
OUT = ROOT / "eval_results" / "questions"
RAW = OUT / "source"
NORMALIZED = OUT / "normalized"

AI_APP_URL = "https://www.iguopin.com/job/list?keyword=AI%E5%BA%94%E7%94%A8%E5%BC%80%E5%8F%91"
AI_AGENT_URLS = [
    "https://www.liepin.com/zpdmxyykfgcsz24g/",
    "https://www.iguopin.com/job/list?keyword=%E5%A4%A7%E6%A8%A1%E5%9E%8B%E5%BA%94%E7%94%A8%E5%BC%80%E5%8F%91",
]
CAMPUS_URL = "https://career.hebut.edu.cn/correcruit/content/id/78016.html"


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


def role_family(text: str) -> str:
    state = bool(re.search(r"央国企|央企|国企|国家电网|中国移动|国聘网|国聘|校招", text))
    agent = bool(re.search(r"AI\s*Agent|Agent|大模型|AIGC|AI\s*算法|智能体", text, re.I))
    if state:
        return "央国企 AI 应用/Agent 开发工程师"
    if agent:
        return "AI Agent 开发工程师"
    return "AI 应用开发工程师"


def replace_role_phrases(text: str, target: str) -> str:
    if not text:
        return text
    patterns = [
        r"前端开发工程师",
        r"Java\s*后端开发工程师",
        r"Java\s*后端",
        r"AIGC\s*产品经理",
        r"AI\s*产品经理",
        r"产品经理",
        r"AI\s*算法工程师",
        r"大模型应用开发工程师",
    ]
    result = text
    for pattern in patterns:
        result = re.sub(pattern, target, result, flags=re.I)
    return result


def target_urls(target: str) -> list[str]:
    if target.startswith("央国企"):
        return [AI_APP_URL, CAMPUS_URL]
    if target.startswith("AI Agent"):
        return AI_AGENT_URLS[:]
    return [AI_APP_URL]


def normalize_node(
    node: dict, source_id: str, node_id: str, forced_target: str | None = None
) -> tuple[dict, dict]:
    node = json.loads(json.dumps(node, ensure_ascii=False))
    profile = node.setdefault("profile", {})
    original_role = str(profile.get("role", ""))
    # The candidate profile may mention 校招/央企 as background.  Only the
    # requested question and its role label decide whether this is an
    # explicitly state-owned-enterprise target.
    context = " ".join([str(node.get("question", "")), original_role])
    target = forced_target or role_family(context)

    node["question"] = replace_role_phrases(str(node.get("question", "")), target)
    if target not in node["question"]:
        node["question"] = f"目标岗位限定为{target}。" + node["question"]
    profile["role"] = re.sub(r"（.*?）|\(.*?\)", "", target).strip()
    profile["summary"] = replace_role_phrases(str(profile.get("summary", "")), target)

    meta = node.setdefault("meta", {})
    notes: list[str] = []
    # One-day/three-day windows are brittle for public job sources; seven days is testable.
    if meta.get("time_window") in {"recent-1-day", "recent-3-days"}:
        meta["time_window"] = "recent-7-days"
        meta["time_window_text"] = "最近7天"
        notes.append("将过窄的1/3天窗口放宽为7天，以覆盖招聘站点更新延迟")

    urls = [canonical_url(u) for u in target_urls(target)]
    meta["target_role_family"] = target
    meta["target_urls"] = urls
    meta["url_source"] = "normalized_role_default"

    intent = "+".join(sorted(meta.get("skills", []))) or "unknown"
    record = {
        "source_id": source_id,
        "node_id": node_id,
        "target_role_family": target,
        "target_urls": urls,
        "intent": intent,
        "realism_notes": notes,
    }
    return node, record


def normalize_question(data: dict) -> tuple[dict, list[dict]]:
    source_id = str(data["id"])
    if "chain" not in data:
        node, record = normalize_node(data, source_id, source_id)
        return node, [record]
    chain = []
    records = []
    first = data["chain"][0]
    first_profile = first.get("profile", {})
    chain_target = role_family(
        " ".join([str(first.get("question", "")), str(first_profile.get("role", ""))])
    )
    for index, node in enumerate(data["chain"], 1):
        node_id = f"{source_id}-L{index}"
        normalized, record = normalize_node(node, source_id, node_id, forced_target=chain_target)
        chain.append(normalized)
        records.append(record)
    return {"id": source_id, "chain": chain}, records


def dedup_key(records: list[dict], kind: str) -> str:
    roles = sorted({r["target_role_family"] for r in records})
    urls = sorted({u for r in records for u in r["target_urls"]})
    intents = sorted({r["intent"] for r in records})
    return "|".join([kind, ";".join(roles), ";".join(urls), ";".join(intents)])


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    NORMALIZED.mkdir(parents=True, exist_ok=True)
    for stale in NORMALIZED.glob("*.json"):
        stale.unlink()
    for path in sorted(SOURCE.glob("*.json")):
        shutil.copy2(path, RAW / path.name)

    files = [p for p in sorted(SOURCE.glob("*.json")) if p.name != "manifest.json"]
    kept: list[dict] = []
    removed: list[dict] = []
    seen: dict[str, str] = {}
    all_records: list[dict] = []

    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        normalized, records = normalize_question(data)
        kind = "chain" if "chain" in data else "single"
        key = dedup_key(records, kind)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        all_records.extend(records)
        if key in seen:
            removed.append({"source_id": data["id"], "duplicate_of": seen[key], "dedup_key": key})
            continue
        seen[key] = data["id"]
        (NORMALIZED / path.name).write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        kept.append(
            {
                "id": data["id"],
                "kind": kind,
                "source_file": path.name,
                "dedup_key": key,
                "dedup_digest": digest,
                "records": records,
            }
        )

    manifest = {
        "source_dir": str(SOURCE),
        "source_question_count": len(files),
        "kept_count": len(kept),
        "removed_count": len(removed),
        "dedup_policy": "target role family + canonical target URL set + evaluation intent + question kind",
        "kept": kept,
        "removed": removed,
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "dedup_report.json").write_text(
        json.dumps({"kept": kept, "removed": removed, "all_records": all_records}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"source": len(files), "kept": len(kept), "removed": len(removed)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
