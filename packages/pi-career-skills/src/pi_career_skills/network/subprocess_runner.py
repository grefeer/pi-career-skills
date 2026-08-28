"""Whitelisted execution channel for the job-discovery skill scripts.

Port of ``skill/job_discovery/runtime/subprocess_runner.py`` (verbatim).
Security: this is the ONLY way skill scripts may run.  It is deliberately
NOT a generic shell backend (which grants arbitrary shell): only the nine
allowlisted scripts run, cwd is pinned to the skill directory so relative
``output/`` paths resolve, and an injectable ``runner`` seam keeps unit
tests deterministic.  ``_terminate_process_tree`` (source
``job_discovery.py`` 199-221) also lives here so the Playwright worker can
kill its own child process tree.

In this package ``SKILL_DIR`` resolves under the pi package tree where no
The package-local scripts live under ``resources/job_discovery_scripts`` and
are shipped with the wheel; no source-repository path is required at runtime.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = _PACKAGE_ROOT / "resources" / "job_discovery_scripts"

_ALLOWED_SCRIPTS = frozenset({"ocr_image"})
_SCRIPT_TIMEOUT_SEC = 900

# runner: (script_path, parts, *, cwd, stdin, timeout) -> stdout text
_ScriptRunner = Callable[[Path, list[str], Path, str | None, int], str]


def _default_runner(
    script_path: Path,
    parts: list[str],
    *,
    cwd: Path,
    stdin: str | None,
    timeout: int,
) -> str:
    cmd = [sys.executable, str(script_path), *parts]
    child_env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        input=stdin,
        env=child_env,
    )
    out = proc.stdout or ""
    if proc.stderr:
        out += "\n[stderr]\n" + proc.stderr[-2000:]
    return out


def run_skill_script(
    script: str,
    argv: Sequence[str] = (),
    stdin: str = "",
    *,
    runner: _ScriptRunner | None = None,
    cwd: Path | str | None = None,
) -> str:
    """Run one allowlisted skill script; never raises, returns stdout/error.

    ``argv`` is passed directly to ``subprocess.run`` without a shell or
    string re-parsing.  ``cwd`` defaults to ``SKILL_DIR``; script paths always
    resolve under the package-local resource directory.
    """
    if script not in _ALLOWED_SCRIPTS:
        return f"ERROR: script not allowed: {script}"
    script_path = SKILL_DIR / f"{script}.py"
    if not script_path.exists():
        return f"ERROR: script not found at {script_path}"
    resolved_cwd = Path(cwd) if cwd is not None else SKILL_DIR
    try:
        return (runner or _default_runner)(
            script_path,
            list(argv),
            cwd=resolved_cwd,
            stdin=stdin if stdin else None,
            timeout=_SCRIPT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: {script} timed out after {_SCRIPT_TIMEOUT_SEC}s"
    except OSError as exc:
        return f"ERROR: {script} could not start: {exc}"


def _terminate_process_tree(pid: int) -> None:
    """Terminate only the owned render worker and its descendants.

    Verbatim from ``skill/job_discovery/runtime/job_discovery.py`` 199-221:
    Windows uses ``taskkill /PID <pid> /T /F`` (the whole tree), everything
    else kills the process group, falling back to a plain SIGKILL.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
        return
    try:
        import signal

        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        with suppress(ProcessLookupError, OSError):
            os.kill(pid, 9)


__all__ = [
    "SKILL_DIR",
    "run_skill_script",
    "_terminate_process_tree",
]
