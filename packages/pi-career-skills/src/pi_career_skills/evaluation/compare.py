"""Compare pi-py eval results against source baselines.

Implements migration plan §12.2: status tally, per-question diff, and
regression categorization.  ``external_blocked`` codes are tracked
separately from the regression count.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .audit import is_regression_error_code
from .schema import validate_record

#: Error codes that count as external blocks (not regressions).
_EXTERNAL_BLOCKED_CODES: frozenset[str] = frozenset(
    {
        "login_required",
        "captcha",
        "anti_bot",
        "sheet_rate_limited",
        "sheet_call_failed",
    }
)

#: Baseline files to skip when loading a results directory.
_SKIP_FILENAMES: frozenset[str] = frozenset(
    {"launch_manifest.json", "summary.json", "comparison.md"}
)


def merge_results(pi_dir: Path) -> list[dict[str, Any]]:
    """Load and validate all JSON records from *pi_dir*.

    Reads every ``*.json`` file except ``launch_manifest.json`` and
    ``summary.json``.  Validates each via ``validate_record``; fails closed
    on schema errors (prints errors and exits non-zero when called via CLI,
    raises ValueError when called as a library).

    Args:
        pi_dir: Directory containing eval result JSON files.

    Returns:
        List of validated record dicts.

    Raises:
        ValueError: When any record fails schema validation.
    """
    records: list[dict[str, Any]] = []
    errors: list[str] = []

    json_files = sorted(pi_dir.glob("*.json"))
    for jf in json_files:
        if jf.name in _SKIP_FILENAMES:
            continue
        try:
            record = json.loads(jf.read_text(encoding="utf-8"))
            validate_record(record)
            records.append(record)
        except Exception as exc:  # noqa: BLE001 — collect all errors
            errors.append(f"{jf.name}: {exc}")

    if errors:
        error_msg = "Schema validation errors:\n" + "\n".join(errors)
        raise ValueError(error_msg)

    return records


def _is_external_blocked(error_code: str | None) -> bool:
    """Return True when error_code is in the external-blocked set."""
    return bool(error_code and error_code in _EXTERNAL_BLOCKED_CODES)


def _has_qualified_artifact(record: dict[str, Any]) -> bool:
    """Return True when record has at least one skill-complete artifact.

    Used for the ``completion_evidence_unavailable`` regression qualifier:
    if there is a qualified artifact (audit passed for that skill) but the
    result still claims evidence unavailable, it's a regression.
    """
    audit = record.get("audit", {})
    status = audit.get("status")
    return status == "passed"


def _tally_status(records: list[dict[str, Any]]) -> dict[str, int]:
    """Count records by result.status."""
    tally: dict[str, int] = {"succeeded": 0, "waiting_user": 0, "failed": 0}
    for r in records:
        status = r.get("result", {}).get("status", "unknown")
        tally[status] = tally.get(status, 0) + 1
    return tally


def _find_regressions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Identify regression records per plan §12.2.

    A record is a regression when:
    - Its error_code is in the regression set (is_regression_error_code), OR
    - Its error_code is ``completion_evidence_unavailable`` AND at least one
      qualified artifact exists (audit passed).
    - ``external_blocked`` codes are tracked separately, NOT in regression.
    """
    regressions: list[dict[str, Any]] = []
    for r in records:
        error_code = r.get("result", {}).get("error_code")
        if not error_code:
            continue
        if _is_external_blocked(error_code):
            continue  # tracked separately
        if is_regression_error_code(error_code):
            regressions.append(
                {
                    "id": r.get("id", ""),
                    "error_code": error_code,
                    "reason": "regression_error_code",
                }
            )
            continue
        if error_code == "completion_evidence_unavailable" and _has_qualified_artifact(
            r
        ):
            regressions.append(
                {
                    "id": r.get("id", ""),
                    "error_code": error_code,
                    "reason": "evidence_unavailable_with_qualified_artifact",
                }
            )
    return regressions


def _find_external_blocked(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Identify externally-blocked records (separate from regressions)."""
    blocked: list[dict[str, Any]] = []
    for r in records:
        error_code = r.get("result", {}).get("error_code")
        if _is_external_blocked(error_code):
            blocked.append(
                {
                    "id": r.get("id", ""),
                    "error_code": error_code,
                }
            )
    return blocked


def compare_pi_to_source(
    pi_records: list[dict[str, Any]],
    source_nonchain_dir: Path | None = None,
    source_chain_dir: Path | None = None,
) -> dict[str, Any]:
    """Compare pi-py eval records against source baseline records.

    Args:
        pi_records: List of validated pi-py eval records.
        source_nonchain_dir: Optional directory with source non-chain JSONs.
        source_chain_dir: Optional directory with source chain JSONs.

    Returns:
        Comparison dict with status tallies, per-question diff, regressions,
        and external_blocked lists.
    """
    pi_tally = _tally_status(pi_records)
    regressions = _find_regressions(pi_records)
    external_blocked = _find_external_blocked(pi_records)

    # Load source baselines if provided.
    source_records: dict[str, dict[str, Any]] = {}
    missing_source_ids: list[str] = []

    def _load_source(src_dir: Path) -> None:
        for jf in sorted(src_dir.glob("*.json")):
            if jf.name in _SKIP_FILENAMES:
                continue
            try:
                rec = json.loads(jf.read_text(encoding="utf-8"))
                rid = rec.get("id", jf.stem)
                source_records[rid] = rec
            except Exception:  # noqa: BLE001 — skip unreadable
                pass

    if source_nonchain_dir and source_nonchain_dir.exists():
        _load_source(source_nonchain_dir)
    if source_chain_dir and source_chain_dir.exists():
        _load_source(source_chain_dir)

    # Per-question diff.
    per_question: list[dict[str, Any]] = []
    for pi in pi_records:
        pid = pi.get("id", "")
        src = source_records.get(pid)
        pi_status = pi.get("result", {}).get("status", "")
        src_status = src.get("result", {}).get("status", "") if src else None
        status_changed = src_status is not None and pi_status != src_status

        per_question.append(
            {
                "id": pid,
                "pi_status": pi_status,
                "source_status": src_status or "missing",
                "status_changed": status_changed,
                "pi_error_code": pi.get("result", {}).get("error_code"),
                "is_regression": any(
                    r["id"] == pid for r in regressions
                ),
                "is_external_blocked": any(
                    b["id"] == pid for b in external_blocked
                ),
            }
        )
        if src is None and source_records:
            missing_source_ids.append(pid)

    return {
        "pi_tally": pi_tally,
        "total_records": len(pi_records),
        "regressions": regressions,
        "regression_count": len(regressions),
        "external_blocked": external_blocked,
        "external_blocked_count": len(external_blocked),
        "per_question": per_question,
        "missing_source_ids": missing_source_ids,
    }


def render_comparison_md(result: dict[str, Any]) -> str:
    """Render comparison result as a Markdown string."""
    lines: list[str] = []
    lines.append("# Evaluation Comparison Report")
    lines.append("")
    lines.append(f"**Total records:** {result['total_records']}")
    lines.append("")
    lines.append("## Status Tally")
    lines.append("")
    lines.append("| Status | Count |")
    lines.append("|--------|-------|")
    for status in ("succeeded", "waiting_user", "failed"):
        lines.append(f"| {status} | {result['pi_tally'].get(status, 0)} |")
    lines.append("")
    lines.append("## Regression Summary")
    lines.append("")
    lines.append(f"- **Regression count:** {result['regression_count']}")
    lines.append(
        f"- **External blocked count:** {result['external_blocked_count']}"
    )
    lines.append("")
    if result["regressions"]:
        lines.append("### Regressions")
        lines.append("")
        lines.append("| ID | Error Code | Reason |")
        lines.append("|----|-----------|--------|")
        for r in result["regressions"]:
            lines.append(f"| {r['id']} | {r['error_code']} | {r['reason']} |")
        lines.append("")
    if result["external_blocked"]:
        lines.append("### External Blocked (not regressions)")
        lines.append("")
        lines.append("| ID | Error Code |")
        lines.append("|----|-----------|")
        for b in result["external_blocked"]:
            lines.append(f"| {b['id']} | {b['error_code']} |")
        lines.append("")
    if result["missing_source_ids"]:
        lines.append(f"### Missing source baselines ({len(result['missing_source_ids'])})")
        lines.append("")
        for mid in result["missing_source_ids"]:
            lines.append(f"- {mid}")
        lines.append("")
    lines.append("## Per-Question Diff")
    lines.append("")
    lines.append("| ID | Pi Status | Source Status | Changed | Regression | External Blocked |")
    lines.append("|----|-----------|---------------|---------|------------|-----------------|")
    for pq in result["per_question"]:
        lines.append(
            f"| {pq['id']} | {pq['pi_status']} | {pq['source_status']} | "
            f"{'yes' if pq['status_changed'] else 'no'} | "
            f"{'yes' if pq['is_regression'] else 'no'} | "
            f"{'yes' if pq['is_external_blocked'] else 'no'} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    """CLI entry point for compare."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Compare pi-py eval results against source baselines."
    )
    parser.add_argument("--pi-dir", required=True, help="Directory with pi-py eval JSONs")
    parser.add_argument(
        "--source-nonchain", default=None, help="Directory with source non-chain JSONs"
    )
    parser.add_argument(
        "--source-chain", default=None, help="Directory with source chain JSONs"
    )
    parser.add_argument(
        "--out", default="comparison.md", help="Output Markdown file path"
    )
    args = parser.parse_args()

    pi_dir = Path(args.pi_dir)
    try:
        records = merge_results(pi_dir)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    source_nonchain = Path(args.source_nonchain) if args.source_nonchain else None
    source_chain = Path(args.source_chain) if args.source_chain else None

    result = compare_pi_to_source(
        records,
        source_nonchain_dir=source_nonchain,
        source_chain_dir=source_chain,
    )

    md = render_comparison_md(result)
    out_path = Path(args.out)
    out_path.write_text(md, encoding="utf-8")
    print(f"Comparison written to {out_path}")
    print(f"Total: {result['total_records']}, "
          f"Regressions: {result['regression_count']}, "
          f"External blocked: {result['external_blocked_count']}")


__all__ = [
    "merge_results",
    "compare_pi_to_source",
    "render_comparison_md",
    "is_regression_error_code",
    "main",
]
