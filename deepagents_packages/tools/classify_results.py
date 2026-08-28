"""Classify eval-suite results into failure mechanisms.

Reads an eval output directory (--out) of the 25-question suite and emits a
per-question table: outcome, error_code, artifacts, and the dominant mechanism
behind any waiting_user/failed outcome.

Mechanisms (see MIGRATION.md §8 and the session's failure analysis):
  1. ls-loop            — supervisor loops on ls (fixed by excluded_tools)
  2. fallback-blind     — matching fallback ran without projected candidates
                          (fixed by matching projection bridge)
  3. early-return-quality — delegation success without quality-gated evidence
                          (fixed by completion-evidence floor)
  4. killed-delegation  — subagent killed by model-call budget -> supervisor
                          re-delegates forever; timeout skipped the store check
                          (fixed by 4a store-satisfy on timeout + 4b kill-count halt)
  5. empty_public_page  — JS-rendered aggregator list page (iguopin) returns
                          empty content -> hard stall -> no_progress
  6. quality-gate       — fetched pages exist but all fail the JD quality gate
                          -> completion_evidence_unavailable
  7. anti-bot           — public source returned an anti-bot/manual-review error
  8. other              — anything else (runtime error, key missing, ...)

Run: .venv/Scripts/python.exe -m deepagents_packages.tools.classify_results --out temp/eval_real_25_v2
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _link_events(link: dict) -> list[dict]:
    evs: list[dict] = []
    for attempt in link.get("attempts") or []:
        evs.extend(attempt.get("events") or [])
    evs.extend(link.get("events") or [])
    return evs


def _error_codes(link: dict) -> list[str]:
    codes: list[str] = []
    for e in _link_events(link):
        p = e.get("payload") or {}
        code = p.get("error_code")
        if code and code not in codes:
            codes.append(code)
    return codes


def _artifacts(link: dict) -> int:
    return len(link.get("artifacts") or [])


def _job_bearing(link: dict) -> int:
    return sum(
        1
        for a in link.get("artifacts") or []
        if a.get("quality") in {"jd_complete", "jd_partial"}
    )


def _has_seed_iguopin(link: dict) -> bool:
    urls = (link.get("config") or {}).get("seeded_urls") or []
    return any("iguopin.com/job/list" in u for u in urls)


def classify_link(link: dict) -> tuple[str, str]:
    """Return (outcome, mechanism) for a single chain link."""
    result = link.get("result") or {}
    status = result.get("status")
    code = result.get("error_code")
    if status == "succeeded":
        return ("succeeded", "")
    if status == "failed":
        return ("failed", "other")
    # waiting_user
    if code == "no_progress":
        if _has_seed_iguopin(link):
            return ("waiting_user", "5-empty_public_page(iguopin)")
        return ("waiting_user", "no_progress")
    if code == "completion_evidence_unavailable":
        return ("waiting_user", "6-quality_gate")
    if code == "budget_exhausted":
        return ("waiting_user", "4-killed_delegation")
    if code == "wall_clock_budget_exhausted":
        return ("waiting_user", "4-wall_clock")
    if code == "anti_bot_challenge":
        return ("waiting_user", "7-anti_bot")
    if code == "auto_recovery_limit_reached":
        return ("waiting_user", "auto_recovery")
    return ("waiting_user", f"8-other({code})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    files = sorted(args.out.glob("*.json"))
    if not files:
        print(f"没有结果文件: {args.out}")
        return

    rows: list[tuple[str, str, str, int, int, list[str]]] = []
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        qid = d.get("id") or f.stem
        links = d.get("links") or []
        link_rows = []
        for link in links:
            outcome, mech = classify_link(link)
            codes = _error_codes(link)
            link_rows.append(
                (link.get("id", qid), outcome, mech, _artifacts(link), _job_bearing(link), codes)
            )
        for lr in link_rows:
            rows.append((qid, *lr))

    print(f"{'qid':7s} {'link':10s} {'status':12s} {'mechanism':38s} {'arts':>4s} {'jb':>3s} codes")
    print("-" * 110)
    for qid, lid, status, mech, arts, jb, codes in rows:
        print(
            f"{qid:7s} {lid:10s} {status:12s} {mech:38s} {arts:4d} {jb:3d} {','.join(codes)[:40]}"
        )

    # Chain-level rollup
    by_qid: dict[str, list] = {}
    for qid, lid, status, mech, arts, jb, codes in rows:
        by_qid.setdefault(qid, []).append((status, mech, arts, jb))
    print()
    print(f"{'qid':7s} {'links':6s} {'best':12s} mechanisms")
    print("-" * 80)
    for qid in sorted(by_qid):
        lrs = by_qid[qid]
        best = "succeeded" if any(s == "succeeded" for s, *_ in lrs) else lrs[0][0]
        mechs = ",".join(sorted({m for _, m, _, _ in lrs if m}))
        print(f"{qid:7s} {len(lrs):6d} {best:12s} {mechs}")


if __name__ == "__main__":
    main()
