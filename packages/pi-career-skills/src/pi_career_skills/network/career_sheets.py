"""Query the canonical Tencent smartsheet career records as an evidence source.

Verbatim port of ``skill/job_discovery/runtime/career_sheets.py``.  The two
recruitment smartsheets (Sheet A "27届提前批秋招信息汇总", Sheet B
"27届校招秋招实习内推合集") are the primary candidate-URL source for
job-discovery: they carry 内推/招聘 links with company, industry, location and
update-time metadata.  Network search remains the fallback when the sheets
hold no matching record.  Each matched record carries a ``prior_metadata``
bundle (company / apply_url / referral_code / update_time) -- smartsheet-carried
facts that can fill fields a job page itself does not display.

Bridge: the smartsheet tools are exposed by the local ``mcporter`` MCP bridge
(``tencent-docs`` server, token from the ``TENCENT_DOCS_TOKEN`` environment
variable).  We call the CLI as a subprocess; any failure degrades to a stable
``SheetQueryError`` code instead of leaking stderr or secrets to the agent.

Pi adaptations: the input/output models live in
``..business.job_discovery.models`` (byte-identical per the §6 contract
snapshot gate); ``SHEET_RATE_LIMITED`` / ``SHEET_CALL_FAILED`` constants come
from ``..errors`` (same string values); the public handler re-raises
``SheetQueryError`` as ``CareerToolError`` so the stable code survives the
adapter boundary (a bare ``SheetQueryError`` would otherwise degrade to
``tool_execution_failed``).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..business.job_discovery.models import (
    CareerSheetPriorMetadata,
    CareerSheetRecord,
    QueryCareerSheetRecordsInput,
    QueryCareerSheetRecordsOutput,
)
from ..context import ToolContext
from ..errors import (
    SHEET_CALL_FAILED,
    SHEET_RATE_LIMITED,
    CareerToolError,
)

# ---------------------------------------------------------------- sheet registry
# file_id / sheet_id come from the smartsheet-sources reference doc. Queried
# tabs: Sheet A 内推信息 (primary) + 招聘推文校招信息 (secondary), Sheet B
# 每日更新 (largest) + 实习内推汇总.
SHEET_REGISTRY: tuple[dict[str, str], ...] = (
    {
        "file_id": "DZkdPVGtGb1ZvaG5R",
        "sheet_id": "t00i2h",
        "name": "27届内推信息(Sheet A)",
    },
    {
        "file_id": "DZkdPVGtGb1ZvaG5R",
        "sheet_id": "tbVCvT",
        "name": "27届招聘推文校招信息(Sheet A)",
    },
    {
        "file_id": "DY3pHYkNvb0ZRSHdi",
        "sheet_id": "tZW9Ng",
        "name": "每日更新(Sheet B)",
    },
    {
        "file_id": "DY3pHYkNvb0ZRSHdi",
        "sheet_id": "BB08J2",
        "name": "实习内推汇总(Sheet B)",
    },
)

_PAGE_SIZE = 50
_MAX_RECORDS_SCANNED_PER_SHEET = 200
_MAX_OUTPUT_RECORDS = 20
_MCP_TIMEOUT_SECONDS = 30
_MILLIS_PER_DAY = 86_400_000

# Bounded retry for transient bridge failures (spawn error, timeout, bad exit
# code, unparsable JSON): the call is retried once after a short delay.
# Rate-limit failures are NEVER retried: the daily quota will not recover
# inside the same run, so retrying only burns time before the same stable
# error.
_MAX_RETRY_ATTEMPTS = 1
_RETRY_DELAY_SECONDS = 1.5

# Rate-limit markers found in the mcporter subprocess output when the Tencent
# docs API daily quota is exhausted (MCP error 400007 "access limit"). Any hit
# classifies the failure as ``sheet_rate_limited``: a stable condition that
# retrying in-run cannot recover.
_RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "400007",
    "access limit",
    "访问限制",
    "quota",
    "limit",
    "频率",
    "超过",
)

# Executor-facing message attached to a ``sheet_rate_limited`` failure. Kept
# factual: it names the authorized fallback source when the smartsheet API
# cannot serve the query, so the executor switches instead of re-issuing the
# doomed call.
_SHEET_RATE_LIMITED_MESSAGE = (
    "sheet_rate_limited: Tencent smartsheet API 今日访问限制已达上限(400007 access limit)，"
    "配额在本轮运行内不会恢复；search-public-job-pages 是授权的备用数据源，应切换到公开搜索。"
)


class SheetQueryError(Exception):
    """Stable, non-sensitive smartsheet bridge failure.

    ``code`` is the stable error code the harness maps into a ToolObservation
    (``sheet_rate_limited`` / ``sheet_call_failed`` / ``sheet_bridge_unavailable``).
    The optional ``message`` carries executor-facing guidance (for example the
    authorized fallback when the API is rate-limited) and defaults to the bare
    code, so a plain ``str(exc)`` stays the stable, non-sensitive code.
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


# ------------------------------------------------------------------- bridge
def _is_rate_limited_output(stdout: str, stderr: str) -> bool:
    """True when the mcporter output carries a Tencent rate-limit marker.

    The bridge reports the exhausted daily quota as MCP error 400007
    "access limit" on stderr; ``quota``/``limit`` and the Chinese markers
    cover related wordings. The scan is case-insensitive for ASCII markers.
    """
    haystack = f"{stdout} {stderr}".lower()
    return any(marker.lower() in haystack for marker in _RATE_LIMIT_MARKERS)


def _default_list_records_impl(
    file_id: str, sheet_id: str, limit: int, offset: int
) -> dict[str, Any]:
    """Call ``mcporter call tencent-docs smartsheet.list_records``.

    Transient transport/parse failures (spawn error, timeout, bad exit code,
    unparsable JSON) are retried once after ``_RETRY_DELAY_SECONDS``. A
    rate-limit marker in the subprocess output (Tencent MCP error 400007
    "access limit", the exhausted daily quota) raises ``sheet_rate_limited``
    immediately and is NEVER retried: the quota will not recover inside the
    run, so the executor gets the stable failure plus the authorized
    fallback instead of burning a retry.
    """
    mcporter = shutil.which("mcporter")
    if not mcporter:
        raise SheetQueryError("sheet_bridge_unavailable")
    cmd = [
        mcporter,
        "call",
        "tencent-docs",
        "smartsheet.list_records",
        f"file_id={file_id}",
        f"sheet_id={sheet_id}",
        f"limit={limit}",
        f"offset={offset}",
    ]
    # mcporter resolves to a .CMD shim on Windows, which CreateProcess cannot
    # launch directly; route it through cmd.exe.
    if mcporter.lower().endswith(".cmd"):
        cmd = ["cmd", "/c", *cmd]
    for attempt in range(_MAX_RETRY_ATTEMPTS + 1):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_MCP_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            # A spawn error or timeout produces no bridge output, so no
            # rate-limit marker can be present: transient by definition,
            # falls through to the bounded retry below.
            pass
        else:
            if proc.returncode != 0:
                if _is_rate_limited_output(
                    getattr(proc, "stdout", ""), getattr(proc, "stderr", "")
                ):
                    raise SheetQueryError(SHEET_RATE_LIMITED, _SHEET_RATE_LIMITED_MESSAGE)
            else:
                try:
                    return json.loads(proc.stdout)
                except json.JSONDecodeError:
                    # The bridge reported success but produced unparsable
                    # output; scan it for a rate-limit marker before treating
                    # it as a transient parse failure.
                    if _is_rate_limited_output(
                        getattr(proc, "stdout", ""), getattr(proc, "stderr", "")
                    ):
                        raise SheetQueryError(
                            SHEET_RATE_LIMITED, _SHEET_RATE_LIMITED_MESSAGE
                        ) from None
        # Only a transient failure (spawn error, timeout, bad exit code, or
        # unparsable output) reaches here: retry once after a short delay,
        # then surface the stable sheet_call_failed.
        if attempt < _MAX_RETRY_ATTEMPTS:
            time.sleep(_RETRY_DELAY_SECONDS)
    raise SheetQueryError(SHEET_CALL_FAILED)


_list_records_impl: Callable[[str, str, int, int], dict[str, Any]] = _default_list_records_impl


def _normalize_field_value(value: Any) -> str | None:
    """Flatten one smartsheet field value into a plain string."""
    if value is None:
        return None
    if isinstance(value, (str, int, float)):
        return str(value)
    if not isinstance(value, dict):
        return None
    items = value.get("items")
    if isinstance(items, list):
        parts: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("text", "link"):
                part = item.get(key)
                if isinstance(part, str) and part:
                    parts.append(part)
        return " ".join(parts) or None
    scalar = value.get("string_value")
    return str(scalar) if scalar is not None else None


def _field_map(field_values: list[dict[str, Any]]) -> dict[str, str]:
    """field name -> flattened value for one record."""
    fields: dict[str, str] = {}
    for entry in field_values:
        if not isinstance(entry, dict):
            continue
        field = entry.get("field")
        if not isinstance(field, str) or not field:
            continue
        normalized = _normalize_field_value(entry.get("text_value"))
        if normalized is None:
            normalized = _normalize_field_value(entry.get("url_value"))
        if normalized is None:
            normalized = _normalize_field_value(entry.get("option_value"))
        if normalized is None:
            normalized = _normalize_field_value(entry.get("string_value"))
        if normalized is not None:
            fields[field] = normalized
    return fields


def _pick_field(fields: dict[str, str], *needles: str) -> str | None:
    for needle in needles:
        for name, value in fields.items():
            if needle in name:
                return value
    return None


# ------------------------------------------------------------- evidence binding
def _canonical_payload(record: CareerSheetRecord) -> dict[str, Any]:
    """Record content minus derived evidence fields, for stable hashing."""
    payload = record.model_dump()
    payload.pop("source_url", None)
    payload.pop("content_hash", None)
    return payload


def _content_hash_of(payload: Any) -> str:
    """Stable sha256 over canonical JSON (mirrors runtime skill-artifact hashing)."""
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _updated_within_days(updated_raw: str | None, recent_days: int | None) -> bool:
    """True when the record's update time falls inside the requested window."""
    if recent_days is None:
        return True
    if not updated_raw:
        return False
    raw = updated_raw.strip()
    try:
        if len(raw) == 13 and raw.isdigit():
            timestamp_ms = int(raw)
        elif len(raw) == 10 and raw.isdigit():
            timestamp_ms = int(raw) * 1000
        else:
            parsed = datetime.strptime(raw[:10], "%Y-%m-%d")
            timestamp_ms = int(parsed.replace(tzinfo=UTC).timestamp() * 1000)
    except ValueError:
        return False
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    return now_ms - timestamp_ms < recent_days * _MILLIS_PER_DAY


def _record_matches(
    fields: dict[str, str],
    company_keywords: list[str],
    role_keywords: list[str],
    location_keywords: list[str],
) -> bool:
    """Substring match over company, industry, location and summary text."""
    company = _pick_field(fields, "企业", "公司") or ""
    industry = _pick_field(fields, "行业", "类型") or ""
    location = _pick_field(fields, "地点", "城市") or ""
    summary = _pick_field(fields, "文案", "职位", "岗位") or ""
    haystack = f"{company} {industry} {location} {summary}".lower()
    for keywords in (company_keywords, role_keywords, location_keywords):
        if keywords and not any(kw.strip().lower() in haystack for kw in keywords):
            return False
    return True


def _scan_sheet(
    sheet: dict[str, str],
    payload: QueryCareerSheetRecordsInput,
    output: list[CareerSheetRecord],
) -> tuple[int, bool]:
    """Paginate one tab, match + time-filter, append up to the global cap.

    Returns (records_scanned, stopped_at_scan_cap). Scanning stops when the
    tab runs out (has_more), the per-sheet scan cap is hit, or the output cap
    is reached.
    """
    scanned = 0
    offset = 0
    while True:
        if scanned >= _MAX_RECORDS_SCANNED_PER_SHEET or len(output) >= _MAX_OUTPUT_RECORDS:
            return scanned, scanned >= _MAX_RECORDS_SCANNED_PER_SHEET
        response = _list_records_impl(
            sheet["file_id"], sheet["sheet_id"], _PAGE_SIZE, offset
        )
        records = response.get("records")
        if not isinstance(records, list):
            records = []
        for entry in records:
            if len(output) >= _MAX_OUTPUT_RECORDS:
                break
            if not isinstance(entry, dict):
                continue
            fields = _field_map(entry.get("field_values") or [])
            updated_raw = _pick_field(fields, "更新", "时间")
            if not _updated_within_days(updated_raw, payload.recent_days):
                continue
            if not _record_matches(
                fields, payload.company_keywords, payload.role_keywords, payload.location_keywords
            ):
                continue
            company_name = _pick_field(fields, "企业", "公司")
            # Apply URL columns are 内推链接/招聘链接/投递链接 per the source
            # doc; the bare 内推 fallback is dropped so a referral-code column
            # (内推码) can never be captured as the apply URL.
            apply_url = _pick_field(fields, "链接", "投递")
            record = CareerSheetRecord(
                company_name=company_name,
                apply_url=apply_url,
                sheet_name=sheet["name"],
                industry=_pick_field(fields, "行业", "类型"),
                location=_pick_field(fields, "地点", "城市"),
                recruitment_type=_pick_field(fields, "招聘", "内推"),
                updated_at=updated_raw,
                raw_summary=(_pick_field(fields, "文案") or "")[:200] or None,
                prior_metadata=CareerSheetPriorMetadata(
                    company_name=company_name,
                    apply_url=apply_url,
                    referral_code=_pick_field(fields, "内推码"),
                    update_time=updated_raw,
                ),
            )
            if apply_url is not None:
                record = record.model_copy(
                    update={
                        "source_url": apply_url,
                        "content_hash": _content_hash_of(_canonical_payload(record)),
                    }
                )
            output.append(record)
        scanned += len(records)
        if not response.get("has_more"):
            return scanned, False
        next_offset = response.get("next")
        offset = next_offset if isinstance(next_offset, int) else offset + len(records)


# ------------------------------------------------------------------- handler
def query_career_sheet_records(
    context: ToolContext, payload: QueryCareerSheetRecordsInput
) -> QueryCareerSheetRecordsOutput:
    """Return bounded, keyword/time-filtered recruitment records with URLs.

    Bridge failures are re-raised as ``CareerToolError`` carrying the stable
    ``SheetQueryError.code`` (``sheet_rate_limited`` / ``sheet_call_failed`` /
    ``sheet_bridge_unavailable``); the adapter boundary then surfaces that
    code instead of degrading it to a generic ``tool_execution_failed``.
    """
    del context
    try:
        return _query_career_sheet_records(payload)
    except SheetQueryError as exc:
        message = (
            _SHEET_RATE_LIMITED_MESSAGE
            if exc.code == SHEET_RATE_LIMITED and str(exc) == SHEET_RATE_LIMITED
            else str(exc)
        )
        raise CareerToolError(exc.code, message) from exc


def _query_career_sheet_records(
    payload: QueryCareerSheetRecordsInput,
) -> QueryCareerSheetRecordsOutput:
    records: list[CareerSheetRecord] = []
    scanned_total = 0
    sheets_queried = 0
    truncated = False
    for sheet in SHEET_REGISTRY:
        if len(records) >= _MAX_OUTPUT_RECORDS:
            break
        sheets_queried += 1
        scanned, stopped_at_cap = _scan_sheet(sheet, payload, records)
        scanned_total += scanned
        truncated = truncated or stopped_at_cap
    first_source_url = next(
        (record.source_url for record in records if record.source_url), None
    )
    return QueryCareerSheetRecordsOutput(
        records=records,
        matched_count=len(records),
        scanned_count=scanned_total,
        sheets_queried=sheets_queried,
        truncated=truncated or len(records) >= _MAX_OUTPUT_RECORDS,
        query={
            "company_keywords": payload.company_keywords,
            "role_keywords": payload.role_keywords,
            "location_keywords": payload.location_keywords,
            "recent_days": payload.recent_days,
        },
        source_url=first_source_url or f"https://docs.qq.com/sheet/{SHEET_REGISTRY[0]['file_id']}",
        content_hash=_content_hash_of([_canonical_payload(record) for record in records]),
    )


__all__ = [
    "SHEET_REGISTRY",
    "_PAGE_SIZE",
    "_MAX_RECORDS_SCANNED_PER_SHEET",
    "_MAX_OUTPUT_RECORDS",
    "_MCP_TIMEOUT_SECONDS",
    "_MILLIS_PER_DAY",
    "_MAX_RETRY_ATTEMPTS",
    "_RETRY_DELAY_SECONDS",
    "_RATE_LIMIT_MARKERS",
    "_SHEET_RATE_LIMITED_MESSAGE",
    "SheetQueryError",
    "_is_rate_limited_output",
    "_default_list_records_impl",
    "_list_records_impl",
    "_normalize_field_value",
    "_field_map",
    "_pick_field",
    "_canonical_payload",
    "_content_hash_of",
    "_updated_within_days",
    "_record_matches",
    "_scan_sheet",
    "query_career_sheet_records",
]
