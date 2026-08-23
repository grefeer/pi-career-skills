"""One-shot Playwright render worker + bounded fallback renderer.

Ports of ``skill/job_discovery/runtime/playwright_worker.py`` (verbatim) and
``skill/job_discovery/runtime/job_discovery.py``:

- ``_render`` / ``main`` (worker subprocess, 17-97);
- ``_render_metadata`` / ``_set_render_metadata`` (171-180);
- ``_playwright_worker_command`` (183-196);
- ``_render_with_playwright_process`` (222-285) -- the killable child process
  with the hard ``_RENDER_TIMEOUT_S`` deadline;
- ``enable_playwright_fallback`` (288-291) -- Playwright is OFF by default and
  never loads a login profile;
- ``_render_with_playwright`` (1481-1636) -- route-guarded render, one
  relaunch-once policy, threaded watchdog bounding.

The worker import stays lazy/guarded so this package works without the
optional ``playwright`` extra installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..business.job_discovery.models import _MIN_USABLE_TEXT_CHARS
from .page_links import _collect_page_links
from .subprocess_runner import _terminate_process_tree
from .url_guard import PublicFetchError, _assert_public_url, _is_public_url

#: Codes a successful requests fetch may still hand to the render fallback
#: (an empty / shell / fetch-failed page is worth one render attempt).
_PLAYWRIGHT_FALLBACK_CODES = frozenset(
    {"public_fetch_failed", "empty_public_page", "public_page_content_insufficient"}
)

#: Playwright fallback is OFF unless runtime assembly opts in.
_PLAYWRIGHT_FALLBACK_ENABLED = False
#: Compatibility reset seam only. Phase 6 never loads a profile.
_PLAYWRIGHT_STORAGE_STATE_PATH: str | None = None

_RENDER_LOCK = threading.Lock()
_RENDER_TIMEOUT_S = 60

#: Unit-test seam: when set, ``_render_with_playwright`` calls it directly
#: instead of launching any browser or subprocess.
_PLAYWRIGHT_FETCH_IMPL: Callable[[str], tuple[str, str | None]] | None = None
#: (sync_playwright, browser) shared runtime; one relaunch per process.
_PLAYWRIGHT_RUNTIME: tuple[Any, Any] | None = None
_RENDER_METADATA = threading.local()


def _render_metadata(url: str) -> tuple[str, int | None]:
    """Read the most recent render's final URL/status without changing tuple APIs."""
    metadata = getattr(_RENDER_METADATA, "value", None)
    if metadata is None:
        return url, 200
    return metadata


def _set_render_metadata(url: str, status_code: int | None) -> None:
    _RENDER_METADATA.value = (url, status_code)


def _playwright_worker_command(url: str, *, collect_links: bool) -> list[str]:
    """Build the isolated render-worker command without shell interpolation."""
    command = [
        sys.executable,
        "-m",
        "pi_career_skills.network.playwright_worker",
        "--url",
        url,
    ]
    if collect_links:
        command.append("--collect-links")
    return command


def _render_with_playwright_process(
    url: str, *, collect_links: bool
) -> tuple[str, str | None] | tuple[str, str | None, list[str]]:
    """Render in a killable child process so Chromium cannot become an orphan."""
    _assert_public_url(url)
    kwargs: dict[str, Any] = {
        "cwd": str(Path(__file__).resolve().parents[3]),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(
        _playwright_worker_command(url, collect_links=collect_links), **kwargs
    )
    try:
        stdout, _stderr = process.communicate(timeout=_RENDER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process.pid)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise PublicFetchError("public_fetch_failed") from None
    if process.returncode != 0:
        raise PublicFetchError("public_fetch_failed") from None
    try:
        payload = json.loads((stdout or "").strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        raise PublicFetchError("public_fetch_failed") from None
    if payload.get("error"):
        raise PublicFetchError(
            str(payload["error"]),
            effective_url=payload.get("effective_url")
            if isinstance(payload.get("effective_url"), str)
            else url,
            status_code=payload.get("status_code")
            if isinstance(payload.get("status_code"), int)
            else None,
        )
    body = payload.get("body")
    title = payload.get("title")
    effective_url = payload.get("effective_url")
    status_code = payload.get("status_code")
    _set_render_metadata(
        effective_url if isinstance(effective_url, str) else url,
        status_code if isinstance(status_code, int) else 200,
    )
    if not isinstance(body, str):
        raise PublicFetchError("public_fetch_failed")
    if collect_links:
        links = payload.get("links")
        return body, title if isinstance(title, str) else None, (
            links if isinstance(links, list) else []
        )
    return body, title if isinstance(title, str) else None


def enable_playwright_fallback(enabled: bool) -> None:
    """Toggle the rendered-fetch fallback (called from runtime assembly)."""
    global _PLAYWRIGHT_FALLBACK_ENABLED
    _PLAYWRIGHT_FALLBACK_ENABLED = enabled


def configure_playwright_storage_state(path: str | None) -> None:
    """Ignore profile paths: Phase 6 is a public-web-only renderer."""
    del path
    global _PLAYWRIGHT_STORAGE_STATE_PATH
    _PLAYWRIGHT_STORAGE_STATE_PATH = None


def _render_with_playwright(
    url: str, *, collect_links: bool = False
) -> tuple[str, str | None] | tuple[str, str | None, list[str]]:
    """Render ``url`` in headless Chromium; return (body_text, title[, links]).

    Uses the seam ``_PLAYWRIGHT_FETCH_IMPL`` when injected (unit tests); the
    real path lazily imports playwright and reuses one browser per process.
    A per-request route guard aborts any request whose destination is not a
    global public address, mirroring ``_assert_public_url`` inside the
    rendered page (SPA redirects and fetch() subresources included).
    With ``collect_links=True`` the rendered DOM's same-host job-shaped
    ``<a href>`` targets are also returned (list-page expansion, P2).

    The real path relaunches the shared browser exactly once (RC-A): a
    generic crash / OOM / CDP-disconnect mid-render tears the dead runtime
    down and retries, so one dead browser can never fail every later render
    in the process.  A raised ``PublicFetchError`` is never retried --
    security/validation rejections stay final.
    """
    if _PLAYWRIGHT_FETCH_IMPL is not None:
        rendered = _PLAYWRIGHT_FETCH_IMPL(url)
        _set_render_metadata(url, 200)
        body, title = rendered[:2]
        if collect_links:
            return body, title, list(rendered[2]) if len(rendered) >= 3 else []
        return body, title
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise PublicFetchError("public_fetch_failed") from None

    # Real Playwright runs in an isolated one-shot process. Unit tests can
    # still inject the sync API seam; injected fakes remain in-process so the
    # existing deterministic browser contract tests do not need a real browser.
    if getattr(sync_playwright, "__module__", "").startswith("playwright"):
        with _RENDER_LOCK:
            return _render_with_playwright_process(url, collect_links=collect_links)

    def _render_once(browser: Any, target_url: str) -> tuple[Any, ...]:
        """One render pass against ``browser``; the caller owns retry policy."""
        page = browser.new_page()

        def _abort_non_public(route: Any, request: Any) -> None:
            try:
                if _is_public_url(request.url):
                    route.continue_()
                else:
                    route.abort()
            except Exception:
                route.abort()

        page.route("**/*", _abort_non_public)
        try:
            response = page.goto(
                target_url, wait_until="domcontentloaded", timeout=20_000
            )
            if response is None:
                raise PublicFetchError("public_fetch_failed")
            # SPA career portals frequently finish rendering long after
            # domcontentloaded (deferred data fetch + re-render timers; the
            # Feishu campus portal paints its job list ~10s late). Poll the
            # body text until it stops growing above the usable-text threshold,
            # capped at ~15s, so a late-rendering page is not returned as an
            # empty shell. A below-threshold shell must NOT break early: it is
            # exactly the pre-render state we are waiting out.
            page.wait_for_timeout(1_500)
            body_text = page.inner_text("body") or ""
            stable_samples = 0
            for _ in range(30):
                previous_len = len(body_text.strip())
                page.wait_for_timeout(500)
                body_text = page.inner_text("body") or ""
                current_len = len(body_text.strip())
                if (
                    current_len >= _MIN_USABLE_TEXT_CHARS
                    and current_len == previous_len
                ):
                    stable_samples += 1
                    if stable_samples >= 2:
                        break
                else:
                    stable_samples = 0
            title = page.title() or None
            if not collect_links:
                return body_text, title
            return body_text, title, _collect_page_links(page, target_url)
        finally:
            page.close()

    def _run_render() -> tuple[Any, ...]:
        """Launch-or-reuse the shared browser and render; relaunch once."""
        global _PLAYWRIGHT_RUNTIME
        attempt = 0
        while True:
            pw, browser = _PLAYWRIGHT_RUNTIME or (None, None)
            if browser is None:
                try:
                    pw = sync_playwright().start()
                    browser = pw.chromium.launch(headless=True)
                    _PLAYWRIGHT_RUNTIME = (pw, browser)
                except Exception:
                    if pw is not None:
                        with suppress(Exception):
                            pw.stop()
                    raise PublicFetchError("public_fetch_failed") from None
            try:
                return _render_once(browser, url)
            except PublicFetchError:
                raise
            except Exception:
                # The shared browser died mid-render (crash / OOM / CDP
                # disconnect). Every later attempt against the dead runtime
                # would fail in ~0.0s, so tear it down and relaunch exactly
                # once (RC-A). A PublicFetchError is never retried:
                # security/validation rejections and the deliberate
                # blocked-page path stay final.
                if attempt >= 1:
                    raise PublicFetchError("public_fetch_failed") from None
                attempt += 1
                with suppress(Exception):
                    browser.close()
                with suppress(Exception):
                    pw.stop()
                _PLAYWRIGHT_RUNTIME = None

    # Serialize renders (playwright sync is thread-affine; the C5 batch fetch
    # runs 4 threads) and bound each call with a hard watchdog. A wedged
    # driver must not hang the whole eval process: the worker is a daemon,
    # the runtime is orphaned for the next call to replace, and teardown is
    # never attempted from a foreign thread (that call is itself the hang).
    global _PLAYWRIGHT_RUNTIME
    with _RENDER_LOCK:
        boxed: list[tuple[Any, ...]] = []
        errors: list[BaseException] = []

        def _watchdog_target() -> None:
            try:
                boxed.append(_run_render())
            except BaseException as exc:  # re-raised on caller
                errors.append(exc)

        worker = threading.Thread(target=_watchdog_target, daemon=True)
        worker.start()
        worker.join(timeout=_RENDER_TIMEOUT_S)
        if worker.is_alive():
            _PLAYWRIGHT_RUNTIME = None
            raise PublicFetchError("public_fetch_failed")
        if errors:
            raise errors[0]
        return boxed[0]


def _render(url: str, *, collect_links: bool) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            browser_context = browser.new_context()
            page = browser_context.new_page()

            def abort_non_public(route: Any, request: Any) -> None:
                try:
                    if _is_public_url(request.url):
                        route.continue_()
                    else:
                        route.abort()
                except Exception:
                    route.abort()

            page.route("**/*", abort_non_public)
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                if response is None:
                    return {"error": "public_fetch_failed"}
                page.wait_for_timeout(1_500)
                body = page.inner_text("body") or ""
                stable_samples = 0
                for _ in range(30):
                    previous_len = len(body.strip())
                    page.wait_for_timeout(500)
                    body = page.inner_text("body") or ""
                    if (
                        len(body.strip()) >= _MIN_USABLE_TEXT_CHARS
                        and len(body.strip()) == previous_len
                    ):
                        stable_samples += 1
                        if stable_samples >= 2:
                            break
                    else:
                        stable_samples = 0
                result: dict[str, Any] = {
                    "body": body,
                    "title": page.title() or None,
                    "effective_url": page.url,
                    "status_code": response.status,
                }
                if collect_links:
                    result["links"] = _collect_page_links(page, url)
                return result
            finally:
                page.close()
                browser_context.close()
        finally:
            browser.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--collect-links", action="store_true")
    args = parser.parse_args()
    try:
        payload = _render(args.url, collect_links=args.collect_links)
    except PublicFetchError as exc:
        payload = {"error": exc.code}
    except Exception:
        payload = {"error": "public_fetch_failed"}
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()


__all__ = [
    "_PLAYWRIGHT_FALLBACK_CODES",
    "_PLAYWRIGHT_FALLBACK_ENABLED",
    "_PLAYWRIGHT_STORAGE_STATE_PATH",
    "_PLAYWRIGHT_FETCH_IMPL",
    "_PLAYWRIGHT_RUNTIME",
    "_render_metadata",
    "_set_render_metadata",
    "enable_playwright_fallback",
    "configure_playwright_storage_state",
    "_render_with_playwright",
]
