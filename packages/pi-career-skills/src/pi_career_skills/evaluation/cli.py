"""CLI entry point for the evaluation runner.

Handles argument parsing, id validation (fail-closed on missing/duplicate),
and dispatches to ``run_question`` or ``run_chain``.  Multi-worker manifest
mode partitions ids round-robin across subprocess workers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run pi-py career-skills evaluation questions."
    )
    parser.add_argument(
        "--ids", nargs="+", help="Question/chain ids to run (e.g. Q011 C001)"
    )
    parser.add_argument(
        "--manifest", default=None, help="Path to manifest JSON with id list"
    )
    parser.add_argument(
        "--question-dir",
        default="tests/question/redesign",
        help="Directory containing question JSON files",
    )
    parser.add_argument(
        "--out-dir", required=True, help="Output directory for result records"
    )
    parser.add_argument(
        "--model", default="deepseek-v4-flash", help="Model identifier"
    )
    parser.add_argument(
        "--workers", type=int, default=1, help="Number of worker processes"
    )
    parser.add_argument(
        "--stagger-seconds",
        type=int,
        default=90,
        help="Seconds to stagger worker starts",
    )
    parser.add_argument(
        "--enable-playwright-fallback",
        action="store_true",
        help="Enable Playwright when the HTTP fast path is blocked or empty",
    )
    return parser.parse_args(argv)


def _load_ids_from_manifest(manifest_path: Path) -> list[str]:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict) and isinstance(data.get("ids"), list):
        entries = data["ids"]
    else:
        raise ValueError(f"Cannot extract ids from manifest: {manifest_path}")

    if all(isinstance(entry, dict) for entry in entries):
        identifiers = [entry.get("id") for entry in entries]
    elif all(isinstance(entry, str) for entry in entries):
        identifiers = entries
    else:
        raise ValueError(
            f"Cannot extract ids from manifest: {manifest_path} "
            "(mixed or invalid entry formats)"
        )

    ids: list[str] = []
    for index, identifier in enumerate(identifiers):
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError(
                f"Cannot extract ids from manifest: {manifest_path} "
                f"(invalid id at entry {index})"
            )
        ids.append(identifier)
    return ids


def _validate_ids(ids: list[str], question_dir: Path) -> list[str]:
    """Validate ids exist and have no duplicates.

    Returns the de-duplicated list.  Exits non-zero on missing or duplicate.
    """
    # Check duplicates.
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for qid in ids:
        if qid in seen:
            if seen[qid] == 1:
                duplicates.append(qid)
            seen[qid] += 1
        else:
            seen[qid] = 1

    # Check missing files.
    missing: list[str] = []
    for qid in sorted(seen.keys()):
        qpath = question_dir / f"{qid}.json"
        if not qpath.exists():
            missing.append(qid)

    if duplicates or missing:
        msg_parts: list[str] = []
        if duplicates:
            msg_parts.append(f"Duplicate ids: {', '.join(duplicates)}")
        if missing:
            msg_parts.append(f"Missing question files: {', '.join(missing)}")
        print("Error: " + "; ".join(msg_parts), file=sys.stderr)
        sys.exit(1)

    return sorted(seen.keys())


def _partition_ids(ids: list[str], workers: int) -> list[list[str]]:
    """Round-robin partition of ids across workers."""
    partitions: list[list[str]] = [[] for _ in range(workers)]
    for i, qid in enumerate(ids):
        partitions[i % workers].append(qid)
    return [p for p in partitions if p]


def run_cli(argv: list[str] | None = None) -> int:
    """Main CLI entry point.

    Returns exit code (0 for success).
    """
    args = _parse_args(argv)

    question_dir = Path(args.question_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve ids.
    if args.ids:
        ids = list(args.ids)
    elif args.manifest:
        manifest_path = Path(args.manifest)
        ids = _load_ids_from_manifest(manifest_path)
    else:
        print("Error: --ids or --manifest is required", file=sys.stderr)
        return 1

    ids = _validate_ids(ids, question_dir)

    # Single-worker or in-process (--ids mode default).
    workers = max(1, args.workers)

    if workers == 1 or not args.manifest:
        # In-process run for single worker or --ids mode.
        if args.enable_playwright_fallback:
            return asyncio.run(
                _run_in_process(
                    ids,
                    question_dir,
                    out_dir,
                    args.model,
                    enable_playwright=True,
                )
            )
        return asyncio.run(_run_in_process(ids, question_dir, out_dir, args.model))

    # Multi-worker manifest mode: partition + subprocess launch.
    partitions = _partition_ids(ids, workers)
    manifest_record: dict[str, Any] = {
        "total_ids": len(ids),
        "workers": len(partitions),
        "stagger_seconds": args.stagger_seconds,
        "partitions": [
            {"worker": i, "ids": part} for i, part in enumerate(partitions)
        ],
    }
    (out_dir / "launch_manifest.json").write_text(
        json.dumps(manifest_record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    procs: list[subprocess.Popen[str]] = []
    for i, part in enumerate(partitions):
        worker_out = out_dir / f"worker{i}"
        worker_out.mkdir(parents=True, exist_ok=True)
        if i > 0 and args.stagger_seconds > 0:
            time.sleep(args.stagger_seconds)
        cmd = [
            sys.executable,
            "-m",
            "pi_career_skills.evaluation.runner",
            "--ids",
            *part,
            "--question-dir",
            str(question_dir),
            "--out-dir",
            str(worker_out),
            "--model",
            args.model,
        ]
        if args.enable_playwright_fallback:
            cmd.append("--enable-playwright-fallback")
        proc = subprocess.Popen(cmd, text=True)
        procs.append(proc)

    # Wait for all workers.
    for proc in procs:
        proc.wait()

    return 0 if all(p.returncode == 0 for p in procs) else 1


async def _run_in_process(
    ids: list[str],
    question_dir: Path,
    out_dir: Path,
    model_id: str,
    *,
    enable_playwright: bool = False,
) -> int:
    """Run a list of ids in-process (deterministic, for single-worker mode)."""
    if enable_playwright:
        from ..network.playwright_worker import enable_playwright_fallback

        enable_playwright_fallback(True)

    from .chain import run_chain
    from .runner import run_question

    for qid in ids:
        qpath = question_dir / f"{qid}.json"
        doc = json.loads(qpath.read_text(encoding="utf-8"))
        if "chain" in doc:
            await run_chain(
                qid,
                question_dir=question_dir,
                out_dir=out_dir,
                model_id=model_id,
            )
        else:
            await run_question(
                qid,
                question_dir=question_dir,
                out_dir=out_dir,
                model_id=model_id,
            )
        print(f"RUN {qid}: done", flush=True)
    return 0


def main() -> None:
    """Module entry point."""
    sys.exit(run_cli())


if __name__ == "__main__":
    main()


__all__ = [
    "run_cli",
    "main",
    "_validate_ids",
    "_partition_ids",
    "_load_ids_from_manifest",
]
