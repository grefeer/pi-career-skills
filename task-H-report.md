# Task H Report: Phase 6 Network Tools

Date: 2026-08-23

## Scope

Wave 3 Task H ports the `job-discovery` public-network tools into
`packages/pi-career-skills`: URL guard, requests and batch fetch, public
search, Playwright fallback, Tencent career sheets, WeChat/OCR gates,
allowlisted subprocess invocation, candidate validation, and deduplication.

## Verification

- Targeted Phase 5–6 tests: `32 passed`.
- Complete package test suite: `244 passed` (`-m "not integration"`).
- `uv run ruff check packages/pi-career-skills`: `All checks passed!`.
- Both staged and unstaged diffs pass `git diff --check`.

## Review findings and repairs

1. Split modules used imports above the package root; corrected them and
   restored test collection.
2. Consumers captured `_PLAYWRIGHT_FALLBACK_ENABLED` at import time; batch
   and single fetch now read the worker module's live toggle.
3. Evidence keeps a full-text SHA-256 while exposing at most 32,000 visible
   characters; document titles are metadata, not evidence text.
4. Bing redirect decoding accepts both documented `u=a1...` and observed
   `a1=...` URL-safe encodings.
5. A rate-limited sheets bridge provides the authorized public-search fallback
   even when a test seam raises the stable code directly.
6. The review found a dormant Playwright `storage_state` channel. It is now a
   no-op compatibility seam; it is omitted from the worker command, CLI and
   browser context. The regression test was observed failing before this fix.

## Limit

`uv run mypy packages/pi-career-skills/src` currently reports 146 strict-type
errors across 21 files, including pre-existing Phase 2 code. The root mypy
configuration does not yet include this package. This is tracked as package
typing debt and is not represented as a passed Wave 3 gate.
