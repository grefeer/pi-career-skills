"""Small URL identity helpers for evaluation seed projections."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def canonical_seed_url(value: object) -> str | None:
    """Normalize harmless URL presentation differences for deduplication."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return raw
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, query, ""))


def dedupe_seed_urls(values: object) -> list[str]:
    """Preserve first-seen display URLs while deduplicating canonical identity."""
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        canonical = canonical_seed_url(value)
        if canonical is None or canonical in seen:
            continue
        seen.add(canonical)
        result.append(str(value).strip())
    return result
