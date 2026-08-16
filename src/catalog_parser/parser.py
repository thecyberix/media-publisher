from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from googleapiclient.discovery import Resource
from googleapiclient.errors import HttpError

SHEET_ID_PATTERN = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")

CATALOG_OUTPUT_FIELDS = (
    "ctLink",
    "ctPubDate",
    "ctDuration",
    "ctTitle",
    "pkgSmLk",
    "pkgLink",
    "pkgTn",
)

DEFAULT_LIMIT = 10
DEFAULT_MIN_DURATION = 0
DEFAULT_MAX_DURATION = 90

TYPE_REEL = "Reel"
TYPE_SHORT = "Short"
TYPE_VIDEO = "Video"
VIDEO_TYPES = (TYPE_REEL, TYPE_SHORT, TYPE_VIDEO)

REEL_MAX_DURATION = 90
SHORT_MAX_DURATION = 180
VIDEO_MIN_DURATION = SHORT_MAX_DURATION + 1
DEFAULT_VIDEO_TYPE = TYPE_REEL
PUB_DATE_FIELD = "ctPubDate"
_SHEET_DATE_EPOCH = datetime(1899, 12, 30)
_NON_DATE_LABELS = frozenset({"pub date", "best yt", "earliest date"})


def extract_sheet_id(value: str) -> str:
    value = value.strip()
    match = SHEET_ID_PATTERN.search(value)
    if match:
        return match.group(1)
    return value


def _normalize_header(header: str) -> str:
    return header.strip()


def _normalize_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return value


def rows_to_records(headers: list[str], rows: list[list[Any]]) -> list[dict[str, Any]]:
    normalized_headers = [_normalize_header(header) for header in headers]

    if not any(normalized_headers):
        raise ValueError("The sheet has no header row.")

    records: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows, start=2):
        if not any(cell not in (None, "") for cell in row):
            continue

        record: dict[str, Any] = {}
        for column_index, header in enumerate(normalized_headers):
            if not header:
                continue
            value = row[column_index] if column_index < len(row) else None
            record[header] = _normalize_cell(value)

        if any(value is not None for value in record.values()):
            record["_row_number"] = row_index
            records.append(record)

    return records


def select_fields(
    records: list[dict[str, Any]],
    fields: tuple[str, ...] = CATALOG_OUTPUT_FIELDS,
) -> list[dict[str, Any]]:
    return [{field: record.get(field) for field in fields} for record in records]


def parse_duration(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def parse_video_type(value: str) -> str:
    normalized = value.strip().casefold()
    for video_type in VIDEO_TYPES:
        if video_type.casefold() == normalized:
            return video_type
    raise ValueError(f"Unsupported video type: {value!r}")


def duration_to_type(duration: int) -> str:
    if duration <= REEL_MAX_DURATION:
        return TYPE_REEL
    if duration <= SHORT_MAX_DURATION:
        return TYPE_SHORT
    return TYPE_VIDEO


def type_duration_bounds(video_type: str) -> tuple[int, int]:
    normalized = parse_video_type(video_type)
    if normalized == TYPE_REEL:
        return DEFAULT_MIN_DURATION, REEL_MAX_DURATION
    if normalized == TYPE_SHORT:
        return REEL_MAX_DURATION + 1, SHORT_MAX_DURATION
    return VIDEO_MIN_DURATION, 9_999_999


def filter_by_video_type(
    records: list[dict[str, Any]],
    video_type: str = DEFAULT_VIDEO_TYPE,
) -> list[dict[str, Any]]:
    target_type = parse_video_type(video_type)
    filtered: list[dict[str, Any]] = []
    for record in records:
        duration = parse_duration(record.get("ctDuration"))
        if duration is None:
            continue
        if duration_to_type(duration) == target_type:
            filtered.append(record)
    return filtered


def parse_pub_date(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        whole_days = int(value)
        fraction = float(value) - whole_days
        parsed = _SHEET_DATE_EPOCH + timedelta(days=whole_days, seconds=round(fraction * 86400))
        return parsed.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        value = str(value)
    stripped = value.strip()
    if not stripped or stripped.casefold() in _NON_DATE_LABELS:
        return None
    if re.fullmatch(r"\d+", stripped):
        return parse_pub_date(int(stripped))
    if stripped.endswith("Z"):
        stripped = stripped[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(stripped)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        pass
    for fmt in ("%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y"):
        try:
            parsed = datetime.strptime(stripped, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def sort_by_pub_date_newest_first(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def sort_key(record: dict[str, Any]) -> tuple[int, float]:
        parsed = parse_pub_date(record.get(PUB_DATE_FIELD))
        if parsed is None:
            return (1, 0.0)
        return (0, -parsed.timestamp())

    return sorted(records, key=sort_key)


def filter_by_duration(
    records: list[dict[str, Any]],
    min_duration: int = DEFAULT_MIN_DURATION,
    max_duration: int = DEFAULT_MAX_DURATION,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for record in records:
        duration = parse_duration(record.get("ctDuration"))
        if duration is None:
            continue
        if min_duration <= duration <= max_duration:
            filtered.append(record)
    return filtered


def tn_is_marked(value: Any) -> bool:
    """True when SM catalog pkgTn is present and not the explicit unmarked ``X``."""
    text = str(value or "").strip()
    return bool(text) and text.upper() != "X"


def filter_by_pkg_tn(
    records: list[dict[str, Any]],
    *,
    require_marked: bool = False,
) -> list[dict[str, Any]]:
    if not require_marked:
        return records
    return [record for record in records if tn_is_marked(record.get("pkgTn"))]


def order_pkg_tn_first(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable-partition so pkgTn-marked rows are scanned before unmarked ones."""
    marked: list[dict[str, Any]] = []
    unmarked: list[dict[str, Any]] = []
    for record in records:
        if tn_is_marked(record.get("pkgTn")):
            marked.append(record)
        else:
            unmarked.append(record)
    return marked + unmarked


def _quote_sheet_name(sheet_name: str) -> str:
    escaped = sheet_name.replace("'", "''")
    return f"'{escaped}'"


def get_first_sheet_title(service: Resource, sheet_id: str) -> str:
    response = (
        service.spreadsheets()
        .get(spreadsheetId=sheet_id, fields="sheets.properties.title")
        .execute()
    )
    sheets = response.get("sheets", [])
    if not sheets:
        raise ValueError("The spreadsheet has no tabs.")
    return sheets[0]["properties"]["title"]


def resolve_range(service: Resource, catalog_id: str) -> str:
    return _quote_sheet_name(get_first_sheet_title(service, catalog_id))


def fetch_catalog_values(service: Resource, catalog_id: str) -> list[list[Any]]:
    range_notation = resolve_range(service, catalog_id)
    try:
        response = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=catalog_id, range=range_notation)
            .execute()
        )
    except HttpError as exc:
        if exc.resp.status in {403, 404}:
            raise ValueError(
                f"Could not read catalog spreadsheet {catalog_id!r} "
                f"(range {range_notation!r}). Check config/workflow_config.json catalog_id "
                "and that the Google account can open the sheet."
            ) from exc
        raise
    return response.get("values", [])


def parse_catalog(
    service: Resource,
    catalog_id: str,
    limit: int = DEFAULT_LIMIT,
    min_duration: int = DEFAULT_MIN_DURATION,
    max_duration: int = DEFAULT_MAX_DURATION,
    video_type: str = DEFAULT_VIDEO_TYPE,
) -> list[dict[str, Any]]:
    values = fetch_catalog_values(service, catalog_id)
    if not values:
        return []

    headers = values[0]
    data_rows = values[1:]
    records = rows_to_records(headers, data_rows)
    records = [record for record in records if record.get("pkgSmLk") is not None]
    records = filter_by_video_type(records, video_type)
    records = filter_by_duration(records, min_duration, max_duration)
    records = select_fields(records)
    records = sort_by_pub_date_newest_first(records)
    records = order_pkg_tn_first(records)
    if limit > 0:
        records = records[:limit]
    return records
