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
import shlex
import subprocess
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = _PACKAGE_ROOT / "resources" / "job_discovery_scripts"


def quote_arg(value: str) -> str:
    """Quote one cli token so ``split_cli_args`` restores it losslessly.

    Windows: double-quote a token that contains whitespace (inverse of the
    quote-aware split below; backslashes stay literal, matching cmd.exe).
    POSIX: ``shlex.quote``.  This lets paths under e.g. ``Program Files``
    travel through the ``cli_args`` string contract intact.
    """
    if os.name != "nt":
        return shlex.quote(value)
    if any(ch in value for ch in (" ", "\t")):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def split_cli_args(cli_args: str) -> list[str]:
    """Losslessly split a cli_args string into tokens.

    Windows: quote-aware whitespace split (double quotes group a segment and
    are removed; backslashes are literal) -- the inverse of ``quote_arg``.
    POSIX: standard ``shlex.split`` (backslash escaping applies, so POSIX
    callers must quote with ``quote_arg``).
    """
    if os.name != "nt":
        return shlex.split(cli_args)
    tokens: list[str] = []
    buf: list[str] = []
    in_quotes = False
    for ch in cli_args:
        if ch == '"':
            in_quotes = not in_quotes
        elif ch in " \t" and not in_quotes:
            if buf:
                tokens.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if in_quotes:
        raise ValueError("unclosed quote in cli_args")  # keep the refuse-to-run safety
    if buf:
        tokens.append("".join(buf))
    return tokens


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
    cli_args: str = "",
    stdin: str = "",
    *,
    runner: _ScriptRunner | None = None,
    cwd: Path | str | None = None,
) -> str:
    """Run one allowlisted skill script; never raises, returns stdout/error.

    ``cwd`` (default ``SKILL_DIR``) redirects the script's own working
    directory -- the run-scoped ``state_dir`` for ``state.py check/mark`` so
    each eval run writes its own ``output/state.json`` instead of the shared
    skill default.  Script paths always resolve under ``SKILL_DIR/scripts``.
    """
    if script not in _ALLOWED_SCRIPTS:
        return f"ERROR: script not allowed: {script}"
    script_path = SKILL_DIR / f"{script}.py"
    if not script_path.exists():
        return f"ERROR: script not found at {script_path}"
    try:
        parts = split_cli_args(cli_args) if cli_args else []
    except ValueError as exc:
        return f"ERROR: could not parse cli_args {cli_args!r}: {exc}"
    resolved_cwd = Path(cwd) if cwd is not None else SKILL_DIR
    try:
        return (runner or _default_runner)(
            script_path,
            parts,
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
    "quote_arg",
    "split_cli_args",
    "run_skill_script",
    "_terminate_process_tree",
]
