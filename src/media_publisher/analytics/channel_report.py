from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from media_publisher.analytics.channel_report_snapshots import (
    SnapshotStore,
    apply_snapshots_to_monthly_metrics,
    capture_channel_report_snapshots,
    load_snapshot_store,
)
from media_publisher.analytics.meta_analytics import (
    MetaAnalyticsError,
    fetch_facebook_monthly_metrics,
    fetch_instagram_monthly_metrics,
    last_complete_month,
    month_key,
)
from media_publisher.analytics.youtube_analytics import (
    YouTubeAnalyticsError,
    fetch_youtube_monthly_metrics_for_client,
    fetch_youtube_monthly_views_for_client,
)
from media_publisher.publishers.meta import MetaClient
from media_publisher.publishers.youtube import YouTubeClient
from media_publisher.sources.google_sheets import (
    GoogleSheetsClient,
    GoogleSheetsError,
    a1_cell,
)


PlatformName = Literal["youtube", "facebook", "instagram"]
ReportLayout = Literal["month_rows", "kpi_dashboard"]


class ChannelReportError(RuntimeError):
    pass


BULGARIAN_MONTHS = {
    "януари": 1,
    "февруари": 2,
    "март": 3,
    "април": 4,
    "май": 5,
    "юни": 6,
    "юли": 7,
    "август": 8,
    "септември": 9,
    "октомври": 10,
    "ноември": 11,
    "декември": 12,
}

ENGLISH_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

MONTH_ABBREV = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

METRIC_LABEL_KEYS = {
    "video views": "video_views",
    "watchtime": "watch_time_hours",
    "total followers": "followers",
    "reach/unique viewers": "reach",
    "total views (post + video)": "total_views",
    "% of new viewers": "new_viewer_pct",
    "% of non followers views": "non_follower_views_pct",
    "% of inorganic views": "inorganic_views_pct",
    "lau views": "lau_views",
    "shorts views": "shorts_views",
}

METRIC_FALLBACKS: dict[tuple[str, str], tuple[str, ...]] = {
    ("youtube", "video_views"): ("total_views", "lau_views"),
    ("youtube", "lau_views"): ("video_views", "total_views"),
    ("youtube", "total_views"): ("video_views",),
    ("facebook", "video_views"): ("total_views",),
    ("facebook", "lau_views"): ("video_views", "total_views"),
    ("facebook", "shorts_views"): ("video_views",),
    ("instagram", "video_views"): ("total_views",),
    ("instagram", "lau_views"): ("video_views", "total_views"),
    ("instagram", "shorts_views"): ("video_views",),
}

SKIP_METRIC_LABELS = {
    "lau planned",
    "lau actual",
    "shorts planned",
    "shorts actual",
}


@dataclass(frozen=True)
class PlatformSection:
    start_row: int
    end_row: int


@dataclass(frozen=True)
class MetricRow:
    row_number: int
    metric_key: str
    label: str


@dataclass(frozen=True)
class MonthColumn:
    year: int
    month: int
    column_index: int


@dataclass(frozen=True)
class ChannelReportMapping:
    spreadsheet_id: str
    sheet_gid: int | None = None
    sheet_title: str | None = None
    layout: ReportLayout = "kpi_dashboard"
    header_row: int = 1
    first_data_row: int = 2
    month_column_index: int = 0
    month_header_row: int = 3
    metric_column_index: int = 5
    metric_label: str = "Views Actual"
    platform_rows: dict[str, int] = field(default_factory=dict)
    platform_sections: dict[str, PlatformSection] = field(default_factory=dict)
    month_data_column_start: int = 6
    month_data_column_end: int = 32
    platform_columns: dict[str, list[str]] = field(default_factory=dict)
    timezone: str = "Europe/Sofia"

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ChannelReportMapping:
        spreadsheet_id = payload.get("spreadsheet_id")
        if not isinstance(spreadsheet_id, str) or not spreadsheet_id.strip():
            raise ChannelReportError("Channel report mapping is missing spreadsheet_id")

        layout_raw = str(payload.get("layout", "kpi_dashboard")).strip().lower()
        if layout_raw not in ("month_rows", "kpi_dashboard"):
            raise ChannelReportError(f"Unsupported channel report layout: {layout_raw!r}")

        platform_columns = payload.get("platform_columns", {})
        if not isinstance(platform_columns, dict):
            raise ChannelReportError("platform_columns must be an object")

        normalized_columns: dict[str, list[str]] = {}
        for platform in ("youtube", "facebook", "instagram"):
            aliases = platform_columns.get(platform, [])
            if isinstance(aliases, str):
                aliases = [aliases]
            if not isinstance(aliases, list):
                raise ChannelReportError(f"platform_columns.{platform} must be a list")
            normalized_columns[platform] = [
                str(alias).strip() for alias in aliases if str(alias).strip()
            ]

        platform_rows_raw = payload.get("platform_rows", {})
        if not isinstance(platform_rows_raw, dict):
            raise ChannelReportError("platform_rows must be an object")
        platform_rows: dict[str, int] = {}
        for platform, row_number in platform_rows_raw.items():
            if platform not in ("youtube", "facebook", "instagram"):
                continue
            if row_number in (None, ""):
                continue
            platform_rows[str(platform)] = int(row_number)

        platform_sections_raw = payload.get("platform_sections", {})
        if not isinstance(platform_sections_raw, dict):
            raise ChannelReportError("platform_sections must be an object")
        platform_sections: dict[str, PlatformSection] = {}
        for platform, section in platform_sections_raw.items():
            if platform not in ("youtube", "facebook", "instagram"):
                continue
            if not isinstance(section, dict):
                raise ChannelReportError(f"platform_sections.{platform} must be an object")
            start_row = section.get("start_row")
            end_row = section.get("end_row")
            if start_row in (None, "") or end_row in (None, ""):
                continue
            platform_sections[str(platform)] = PlatformSection(
                start_row=int(start_row),
                end_row=int(end_row),
            )

        month_column = payload.get("month_column_index", payload.get("month_column", 0))
        if isinstance(month_column, str):
            month_column = _column_letters_to_index(month_column)

        return cls(
            spreadsheet_id=spreadsheet_id.strip(),
            sheet_gid=_optional_int(payload.get("sheet_gid")),
            sheet_title=_optional_str(payload.get("sheet_title")),
            layout=layout_raw,  # type: ignore[arg-type]
            header_row=int(payload.get("header_row", 1)),
            first_data_row=int(payload.get("first_data_row", 2)),
            month_column_index=int(month_column),
            month_header_row=int(payload.get("month_header_row", 3)),
            metric_column_index=int(payload.get("metric_column_index", 5)),
            metric_label=str(payload.get("metric_label", "Views Actual")).strip()
            or "Views Actual",
            platform_rows=platform_rows,
            platform_sections=platform_sections,
            month_data_column_start=int(payload.get("month_data_column_start", 6)),
            month_data_column_end=int(payload.get("month_data_column_end", 32)),
            platform_columns=normalized_columns,
            timezone=str(payload.get("timezone", "Europe/Sofia")).strip() or "Europe/Sofia",
        )


@dataclass(frozen=True)
class ReportRow:
    row_number: int
    year: int
    month: int


@dataclass(frozen=True)
class ChannelReportUpdate:
    row_number: int
    platform: PlatformName
    views: int
    cell: str
    year: int
    month: int
    metric_key: str = "video_views"


@dataclass(frozen=True)
class ChannelReportResult:
    updates: list[ChannelReportUpdate]
    skipped_cells: list[str]


def load_channel_report_mapping(path: Path) -> ChannelReportMapping:
    if not path.is_file():
        raise ChannelReportError(f"Channel report mapping file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ChannelReportError("Channel report mapping file is invalid")
    return ChannelReportMapping.from_dict(payload)


def inspect_channel_report_sheet(
    client: GoogleSheetsClient,
    mapping: ChannelReportMapping,
    *,
    max_rows: int = 40,
) -> list[list[str]]:
    sheet_title = client.resolve_sheet_title(
        mapping.spreadsheet_id,
        sheet_gid=mapping.sheet_gid,
        sheet_title=mapping.sheet_title,
    )
    escaped_title = sheet_title.replace("'", "''")
    if mapping.layout == "kpi_dashboard":
        month_columns = _load_month_columns(client, mapping, sheet_title)
        last_column = max(column.column_index for column in month_columns)
        end_column = _column_index_to_letters(last_column + 1)
        return client.get_values(
            mapping.spreadsheet_id,
            f"'{escaped_title}'!A1:{end_column}{max_rows}",
        )

    header_rows = client.get_values(
        mapping.spreadsheet_id,
        f"'{escaped_title}'!A{mapping.header_row}:Z{mapping.header_row}",
    )
    headers = header_rows[0] if header_rows else []
    platform_columns = _detect_platform_columns(headers, mapping)
    column_indices = [mapping.month_column_index, *platform_columns.values()]
    last_column = max(column_indices)
    end_column = _column_index_to_letters(last_column + 3)
    return client.get_values(
        mapping.spreadsheet_id,
        f"'{escaped_title}'!A1:{end_column}{max_rows}",
    )


def update_channel_report(
    *,
    mapping: ChannelReportMapping,
    sheets_client: GoogleSheetsClient,
    youtube_client: YouTubeClient | None,
    youtube_channel_id: str | None,
    meta_client: MetaClient | None,
    meta_page_id: str | None,
    meta_instagram_account_id: str | None,
    dry_run: bool = False,
    target_month: date | None = None,
    all_months: bool = False,
    recent_months: bool = False,
    snapshot_store_path: Path | None = None,
    capture_snapshots: bool = False,
) -> ChannelReportResult:
    if mapping.layout == "kpi_dashboard":
        return _update_kpi_dashboard_report(
            mapping=mapping,
            sheets_client=sheets_client,
            youtube_client=youtube_client,
            youtube_channel_id=youtube_channel_id,
            meta_client=meta_client,
            meta_page_id=meta_page_id,
            meta_instagram_account_id=meta_instagram_account_id,
            dry_run=dry_run,
            target_month=target_month,
            all_months=all_months,
            recent_months=recent_months,
            snapshot_store_path=snapshot_store_path,
            capture_snapshots=capture_snapshots,
        )
    return _update_month_rows_report(
        mapping=mapping,
        sheets_client=sheets_client,
        youtube_client=youtube_client,
        youtube_channel_id=youtube_channel_id,
        meta_client=meta_client,
        meta_page_id=meta_page_id,
        meta_instagram_account_id=meta_instagram_account_id,
        dry_run=dry_run,
        target_month=target_month,
        all_months=all_months,
    )


def _update_kpi_dashboard_report(
    *,
    mapping: ChannelReportMapping,
    sheets_client: GoogleSheetsClient,
    youtube_client: YouTubeClient | None,
    youtube_channel_id: str | None,
    meta_client: MetaClient | None,
    meta_page_id: str | None,
    meta_instagram_account_id: str | None,
    dry_run: bool,
    target_month: date | None,
    all_months: bool,
    recent_months: bool = False,
    snapshot_store_path: Path | None = None,
    capture_snapshots: bool = False,
) -> ChannelReportResult:
    if not mapping.platform_sections and not mapping.platform_rows:
        raise ChannelReportError(
            "platform_sections or platform_rows is required for kpi_dashboard layout"
        )

    sheet_title = sheets_client.resolve_sheet_title(
        mapping.spreadsheet_id,
        sheet_gid=mapping.sheet_gid,
        sheet_title=mapping.sheet_title,
    )
    month_columns = _load_month_columns(sheets_client, mapping, sheet_title)
    if not month_columns:
        raise ChannelReportError("No month columns found in the report header row")

    complete_through = last_complete_month()
    if capture_snapshots and snapshot_store_path is not None and not dry_run:
        capture_channel_report_snapshots(
            store_path=snapshot_store_path,
            meta_client=meta_client,
            meta_page_id=meta_page_id,
            meta_instagram_account_id=meta_instagram_account_id,
            youtube_client=youtube_client,
            youtube_channel_id=youtube_channel_id,
        )

    snapshot_store = (
        load_snapshot_store(snapshot_store_path)
        if snapshot_store_path is not None
        else None
    )

    selected_months = _select_month_columns(
        month_columns,
        complete_through=complete_through,
        target_month=target_month,
        all_months=all_months,
        recent_months=recent_months,
    )
    if not selected_months:
        raise ChannelReportError("No report months matched the update criteria")

    start_month = date(selected_months[0].year, selected_months[0].month, 1)
    end_month = date(selected_months[-1].year, selected_months[-1].month, 1)
    monthly_metrics = _fetch_monthly_metrics(
        mapping=mapping,
        start_month=start_month,
        end_month=end_month,
        youtube_client=youtube_client,
        youtube_channel_id=youtube_channel_id,
        meta_client=meta_client,
        meta_page_id=meta_page_id,
        meta_instagram_account_id=meta_instagram_account_id,
        snapshot_store=snapshot_store,
    )

    metric_targets = _load_platform_metric_targets(
        sheets_client,
        mapping,
        sheet_title,
    )

    updates: list[ChannelReportUpdate] = []
    batch: list[tuple[str, list[list[Any]]]] = []
    for platform, metric_rows in metric_targets.items():
        for metric_row in metric_rows:
            for month_column in selected_months:
                key = month_key(month_column.year, month_column.month)
                raw_value = _resolve_metric_value(
                    monthly_metrics,
                    platform,
                    key,
                    metric_row.metric_key,
                )
                if raw_value is None:
                    continue
                value = _format_report_value(metric_row.metric_key, raw_value)
                cell = a1_cell(sheet_title, metric_row.row_number, month_column.column_index)
                updates.append(
                    ChannelReportUpdate(
                        row_number=metric_row.row_number,
                        platform=platform,  # type: ignore[arg-type]
                        views=value,
                        cell=cell,
                        year=month_column.year,
                        month=month_column.month,
                        metric_key=metric_row.metric_key,
                    )
                )
                batch.append((cell, [[value]]))

    if dry_run:
        return ChannelReportResult(updates=updates, skipped_cells=[])

    _try_ensure_report_write_ranges_unprotected(sheets_client, mapping, metric_targets)
    skipped_cells = _write_report_batch(sheets_client, mapping.spreadsheet_id, batch)
    return ChannelReportResult(updates=updates, skipped_cells=skipped_cells)


def _try_ensure_report_write_ranges_unprotected(
    client: GoogleSheetsClient,
    mapping: ChannelReportMapping,
    metric_targets: dict[str, list[MetricRow]] | None = None,
) -> None:
    try:
        ensure_report_write_ranges_unprotected(client, mapping, metric_targets)
    except GoogleSheetsError:
        return


def find_sheet_wide_protection(
    client: GoogleSheetsClient,
    spreadsheet_id: str,
    *,
    sheet_id: int,
) -> dict[str, Any] | None:
    payload = client.get_spreadsheet(spreadsheet_id)
    for sheet in payload.get("sheets", []):
        properties = sheet.get("properties", {})
        if properties.get("sheetId") != sheet_id:
            continue
        for protected_range in sheet.get("protectedRanges", []):
            if not isinstance(protected_range, dict):
                continue
            grid_range = protected_range.get("range", {})
            if not isinstance(grid_range, dict):
                continue
            if grid_range.keys() != {"sheetId"}:
                continue
            if grid_range.get("sheetId") != sheet_id:
                continue
            return protected_range
    return None


def ensure_report_write_ranges_unprotected(
    client: GoogleSheetsClient,
    mapping: ChannelReportMapping,
    metric_targets: dict[str, list[MetricRow]] | None = None,
) -> list[dict[str, Any]]:
    """Extend sheet-wide protection holes to include KPI dashboard metric rows."""
    if mapping.layout != "kpi_dashboard":
        return []

    row_numbers = _report_row_numbers(mapping, metric_targets)
    if not row_numbers:
        return []

    sheet_title = client.resolve_sheet_title(
        mapping.spreadsheet_id,
        sheet_gid=mapping.sheet_gid,
        sheet_title=mapping.sheet_title,
    )
    payload = client.get_spreadsheet(mapping.spreadsheet_id)
    sheet_id = mapping.sheet_gid
    if sheet_id is None:
        for sheet in payload.get("sheets", []):
            properties = sheet.get("properties", {})
            if properties.get("title") == sheet_title:
                sheet_id = properties.get("sheetId")
                break
    if not isinstance(sheet_id, int):
        raise ChannelReportError("Could not resolve Bulgarian tab sheet id")

    protected_range = find_sheet_wide_protection(
        client,
        mapping.spreadsheet_id,
        sheet_id=sheet_id,
    )
    if protected_range is None:
        return []

    protected_range_id = protected_range.get("protectedRangeId")
    if not isinstance(protected_range_id, int):
        raise ChannelReportError("Sheet-wide protection is missing protectedRangeId")

    existing = protected_range.get("unprotectedRanges", [])
    if not isinstance(existing, list):
        existing = []

    required = [
        _unprotected_row_range(
            sheet_id=sheet_id,
            row_number=row_number,
            start_column=mapping.month_data_column_start,
            end_column=mapping.month_data_column_end,
        )
        for row_number in row_numbers
    ]
    merged = _merge_unprotected_ranges([*existing, *required])
    if merged == existing:
        return merged

    client.batch_update_spreadsheet(
        mapping.spreadsheet_id,
        [
            {
                "updateProtectedRange": {
                    "protectedRange": {
                        "protectedRangeId": protected_range_id,
                        "unprotectedRanges": merged,
                    },
                    "fields": "unprotectedRanges",
                }
            }
        ],
    )
    return merged


def _unprotected_row_range(
    *,
    sheet_id: int,
    row_number: int,
    start_column: int,
    end_column: int,
) -> dict[str, int]:
    return {
        "sheetId": sheet_id,
        "startRowIndex": row_number - 1,
        "endRowIndex": row_number,
        "startColumnIndex": start_column,
        "endColumnIndex": end_column,
    }


def _merge_unprotected_ranges(
    ranges: list[dict[str, Any]],
) -> list[dict[str, int]]:
    seen: set[tuple[int, int, int, int, int]] = set()
    merged: list[dict[str, int]] = []
    for item in ranges:
        if not isinstance(item, dict):
            continue
        sheet_id = item.get("sheetId")
        start_row = item.get("startRowIndex")
        end_row = item.get("endRowIndex")
        start_col = item.get("startColumnIndex")
        end_col = item.get("endColumnIndex")
        if not all(isinstance(value, int) for value in (sheet_id, start_row, end_row, start_col, end_col)):
            continue
        key = (sheet_id, start_row, end_row, start_col, end_col)
        if key in seen:
            continue
        seen.add(key)
        merged.append(
            {
                "sheetId": sheet_id,
                "startRowIndex": start_row,
                "endRowIndex": end_row,
                "startColumnIndex": start_col,
                "endColumnIndex": end_col,
            }
        )
    return merged


def _write_report_batch(
    sheets_client: GoogleSheetsClient,
    spreadsheet_id: str,
    batch: list[tuple[str, list[list[Any]]]],
) -> list[str]:
    if not batch:
        return []
    skipped_cells = sheets_client.batch_update_values_resilient(spreadsheet_id, batch)
    if skipped_cells and len(skipped_cells) == len(batch):
        raise ChannelReportError(
            "Could not write any report cells because they are protected. "
            f"Example cell: {skipped_cells[0]}"
        )
    return skipped_cells


def _update_month_rows_report(
    *,
    mapping: ChannelReportMapping,
    sheets_client: GoogleSheetsClient,
    youtube_client: YouTubeClient | None,
    youtube_channel_id: str | None,
    meta_client: MetaClient | None,
    meta_page_id: str | None,
    meta_instagram_account_id: str | None,
    dry_run: bool,
    target_month: date | None,
    all_months: bool,
) -> ChannelReportResult:
    sheet_title = sheets_client.resolve_sheet_title(
        mapping.spreadsheet_id,
        sheet_gid=mapping.sheet_gid,
        sheet_title=mapping.sheet_title,
    )
    escaped_title = sheet_title.replace("'", "''")
    header_rows = sheets_client.get_values(
        mapping.spreadsheet_id,
        f"'{escaped_title}'!A{mapping.header_row}:Z{mapping.header_row}",
    )
    headers = header_rows[0] if header_rows else []
    platform_columns = _detect_platform_columns(headers, mapping)
    if not platform_columns:
        raise ChannelReportError(
            "Could not locate YouTube/Facebook/Instagram columns in the report header row"
        )

    data_rows = sheets_client.get_values(
        mapping.spreadsheet_id,
        f"'{escaped_title}'!A{mapping.first_data_row}:Z200",
    )
    report_rows = _parse_report_rows(data_rows, mapping)
    if not report_rows:
        raise ChannelReportError("No month rows found in the Bulgarian report tab")

    complete_through = last_complete_month()
    selected_rows = _select_report_rows(
        report_rows,
        complete_through=complete_through,
        target_month=target_month,
        all_months=all_months,
    )
    if not selected_rows:
        raise ChannelReportError("No report months matched the update criteria")

    start_month = date(selected_rows[0].year, selected_rows[0].month, 1)
    end_month = date(selected_rows[-1].year, selected_rows[-1].month, 1)
    monthly_views = _fetch_monthly_views(
        mapping=mapping,
        start_month=start_month,
        end_month=end_month,
        youtube_client=youtube_client,
        youtube_channel_id=youtube_channel_id,
        meta_client=meta_client,
        meta_page_id=meta_page_id,
        meta_instagram_account_id=meta_instagram_account_id,
    )

    updates: list[ChannelReportUpdate] = []
    batch: list[tuple[str, list[list[Any]]]] = []
    for report_row in selected_rows:
        key = month_key(report_row.year, report_row.month)
        for platform, column_index in platform_columns.items():
            views = monthly_views.get(platform, {}).get(key)
            if views is None:
                continue
            cell = a1_cell(sheet_title, report_row.row_number, column_index)
            updates.append(
                ChannelReportUpdate(
                    row_number=report_row.row_number,
                    platform=platform,  # type: ignore[arg-type]
                    views=views,
                    cell=cell,
                    year=report_row.year,
                    month=report_row.month,
                )
            )
            batch.append((cell, [[views]]))

    if dry_run:
        return ChannelReportResult(updates=updates, skipped_cells=[])

    skipped_cells = _write_report_batch(sheets_client, mapping.spreadsheet_id, batch)
    return ChannelReportResult(updates=updates, skipped_cells=skipped_cells)


def _load_month_columns(
    client: GoogleSheetsClient,
    mapping: ChannelReportMapping,
    sheet_title: str,
) -> list[MonthColumn]:
    escaped_title = sheet_title.replace("'", "''")
    header_rows = client.get_values(
        mapping.spreadsheet_id,
        f"'{escaped_title}'!A{mapping.month_header_row}:AZ{mapping.month_header_row}",
    )
    headers = header_rows[0] if header_rows else []
    month_columns: list[MonthColumn] = []
    for index, header in enumerate(headers):
        parsed = parse_month_cell(header)
        if parsed is None:
            continue
        month_columns.append(
            MonthColumn(year=parsed[0], month=parsed[1], column_index=index)
        )
    month_columns.sort(key=lambda item: (item.year, item.month))
    return month_columns


def _select_month_columns(
    month_columns: list[MonthColumn],
    *,
    complete_through: date,
    target_month: date | None,
    all_months: bool,
    recent_months: bool = False,
) -> list[MonthColumn]:
    if target_month is not None:
        if target_month.day != 1:
            raise ChannelReportError("target_month must be the first day of a month")
        return [
            column
            for column in month_columns
            if column.year == target_month.year and column.month == target_month.month
        ]

    if all_months:
        return [
            column
            for column in month_columns
            if date(column.year, column.month, 1) <= complete_through
        ]

    if recent_months:
        current_month = date.today().replace(day=1)
        allowed = {complete_through, current_month}
        return [
            column
            for column in month_columns
            if date(column.year, column.month, 1) in allowed
        ]

    return [
        column
        for column in month_columns
        if column.year == complete_through.year and column.month == complete_through.month
    ]


def _select_report_rows(
    report_rows: list[ReportRow],
    *,
    complete_through: date,
    target_month: date | None,
    all_months: bool,
) -> list[ReportRow]:
    if target_month is not None:
        if target_month.day != 1:
            raise ChannelReportError("target_month must be the first day of a month")
        return [
            row
            for row in report_rows
            if row.year == target_month.year and row.month == target_month.month
        ]

    if all_months:
        return [
            row
            for row in report_rows
            if date(row.year, row.month, 1) <= complete_through
        ]

    return [
        row
        for row in report_rows
        if row.year == complete_through.year and row.month == complete_through.month
    ]


def _fetch_monthly_metrics(
    *,
    mapping: ChannelReportMapping,
    start_month: date,
    end_month: date,
    youtube_client: YouTubeClient | None,
    youtube_channel_id: str | None,
    meta_client: MetaClient | None,
    meta_page_id: str | None,
    meta_instagram_account_id: str | None,
    snapshot_store: SnapshotStore | None = None,
) -> dict[str, dict[str, dict[str, float]]]:
    result: dict[str, dict[str, dict[str, float]]] = {
        "youtube": {},
        "facebook": {},
        "instagram": {},
    }
    enabled_platforms = _enabled_platforms(mapping)

    if youtube_client is not None and youtube_channel_id and "youtube" in enabled_platforms:
        try:
            metrics = fetch_youtube_monthly_metrics_for_client(
                youtube_client,
                channel_id=youtube_channel_id,
                start_month=start_month,
                end_month=_next_month(end_month),
            )
        except YouTubeAnalyticsError as exc:
            raise ChannelReportError(str(exc)) from exc
        _merge_youtube_views_fallback(
            metrics,
            youtube_client=youtube_client,
            channel_id=youtube_channel_id,
            start_month=start_month,
            end_month=end_month,
        )
        result["youtube"] = metrics

    if meta_client is not None and meta_page_id and "facebook" in enabled_platforms:
        try:
            result["facebook"] = fetch_facebook_monthly_metrics(
                meta_client,
                page_id=meta_page_id,
                start_month=start_month,
                end_month=end_month,
            )
        except MetaAnalyticsError as exc:
            raise ChannelReportError(str(exc)) from exc

    if (
        meta_client is not None
        and meta_instagram_account_id
        and "instagram" in enabled_platforms
    ):
        try:
            result["instagram"] = fetch_instagram_monthly_metrics(
                meta_client,
                instagram_account_id=meta_instagram_account_id,
                start_month=start_month,
                end_month=end_month,
            )
        except MetaAnalyticsError as exc:
            raise ChannelReportError(str(exc)) from exc

    apply_snapshots_to_monthly_metrics(
        result,
        snapshot_store,
        start_month=start_month,
        end_month=end_month,
    )
    return result


def _fetch_monthly_views(
    *,
    mapping: ChannelReportMapping,
    start_month: date,
    end_month: date,
    youtube_client: YouTubeClient | None,
    youtube_channel_id: str | None,
    meta_client: MetaClient | None,
    meta_page_id: str | None,
    meta_instagram_account_id: str | None,
) -> dict[str, dict[str, int]]:
    metrics = _fetch_monthly_metrics(
        mapping=mapping,
        start_month=start_month,
        end_month=end_month,
        youtube_client=youtube_client,
        youtube_channel_id=youtube_channel_id,
        meta_client=meta_client,
        meta_page_id=meta_page_id,
        meta_instagram_account_id=meta_instagram_account_id,
    )
    result: dict[str, dict[str, int]] = {
        "youtube": {},
        "facebook": {},
        "instagram": {},
    }
    for platform, monthly in metrics.items():
        for key, values in monthly.items():
            result[platform][key] = int(values.get("video_views", 0))
    return result


def _enabled_platforms(mapping: ChannelReportMapping) -> set[str]:
    platforms: set[str] = set()
    if mapping.platform_sections:
        platforms.update(mapping.platform_sections)
    if mapping.platform_rows:
        platforms.update(mapping.platform_rows)
    if platforms:
        return platforms
    return {
        platform
        for platform, aliases in mapping.platform_columns.items()
        if aliases
    }


def _load_platform_metric_targets(
    client: GoogleSheetsClient,
    mapping: ChannelReportMapping,
    sheet_title: str,
) -> dict[str, list[MetricRow]]:
    if mapping.platform_sections:
        return _load_metric_rows_from_sections(client, mapping, sheet_title)

    metric_rows: dict[str, list[MetricRow]] = {}
    for platform, row_number in mapping.platform_rows.items():
        metric_rows[platform] = [
            MetricRow(row_number=row_number, metric_key="video_views", label="Views Actual")
        ]
    return metric_rows


def _load_metric_rows_from_sections(
    client: GoogleSheetsClient,
    mapping: ChannelReportMapping,
    sheet_title: str,
) -> dict[str, list[MetricRow]]:
    escaped_title = sheet_title.replace("'", "''")
    metric_rows: dict[str, list[MetricRow]] = {}
    for platform, section in mapping.platform_sections.items():
        rows = client.get_values(
            mapping.spreadsheet_id,
            f"'{escaped_title}'!A{section.start_row}:F{section.end_row}",
        )
        platform_rows: list[MetricRow] = []
        for offset, row in enumerate(rows):
            row_number = section.start_row + offset
            if mapping.metric_column_index >= len(row):
                continue
            label = row[mapping.metric_column_index].strip()
            metric_key = metric_key_for_label(label)
            if metric_key is None:
                continue
            platform_rows.append(
                MetricRow(row_number=row_number, metric_key=metric_key, label=label)
            )
        metric_rows[platform] = platform_rows
    return metric_rows


def _report_row_numbers(
    mapping: ChannelReportMapping,
    metric_targets: dict[str, list[MetricRow]] | None = None,
) -> list[int]:
    if metric_targets:
        return sorted(
            {
                metric_row.row_number
                for metric_rows in metric_targets.values()
                for metric_row in metric_rows
            }
        )
    if mapping.platform_sections:
        return sorted(
            {
                row_number
                for section in mapping.platform_sections.values()
                for row_number in range(section.start_row, section.end_row + 1)
            }
        )
    return sorted(set(mapping.platform_rows.values()))


def normalize_metric_label(label: str) -> str:
    text = label.strip().casefold()
    text = re.sub(r"[^\w\s%/+()-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def metric_key_for_label(label: str) -> str | None:
    normalized = normalize_metric_label(label)
    if not normalized or normalized in SKIP_METRIC_LABELS:
        return None
    return METRIC_LABEL_KEYS.get(normalized)


def _format_report_value(metric_key: str, value: float) -> int:
    if metric_key.endswith("_pct"):
        return int(round(value))
    return int(round(value))


def _resolve_metric_value(
    monthly_metrics: dict[str, dict[str, dict[str, float]]],
    platform: str,
    month_key: str,
    metric_key: str,
) -> float | None:
    values = monthly_metrics.get(platform, {}).get(month_key, {})
    direct = values.get(metric_key)
    if direct is not None:
        return direct
    for fallback_key in METRIC_FALLBACKS.get((platform, metric_key), ()):
        fallback_value = values.get(fallback_key)
        if fallback_value is not None:
            return fallback_value
    return None


def _merge_youtube_views_fallback(
    metrics: dict[str, dict[str, float]],
    *,
    youtube_client: YouTubeClient,
    channel_id: str,
    start_month: date,
    end_month: date,
) -> None:
    """Supplement section metrics with the legacy monthly views query (deep YT history)."""
    try:
        rows = fetch_youtube_monthly_views_for_client(
            youtube_client,
            channel_id=channel_id,
            start_month=start_month,
            end_month=_next_month(end_month),
        )
    except YouTubeAnalyticsError:
        return
    for row in rows:
        views = float(row.views)
        if views <= 0:
            continue
        key = month_key(row.year, row.month)
        entry = metrics.setdefault(key, {})
        if not entry.get("video_views"):
            entry["video_views"] = views
        if not entry.get("total_views"):
            entry["total_views"] = views
        if not entry.get("lau_views"):
            entry["lau_views"] = views


def _parse_report_rows(
    rows: list[list[str]],
    mapping: ChannelReportMapping,
) -> list[ReportRow]:
    report_rows: list[ReportRow] = []
    for offset, row in enumerate(rows):
        if mapping.month_column_index >= len(row):
            continue
        parsed = parse_month_cell(row[mapping.month_column_index])
        if parsed is None:
            continue
        report_rows.append(
            ReportRow(
                row_number=mapping.first_data_row + offset,
                year=parsed[0],
                month=parsed[1],
            )
        )
    return report_rows


def parse_month_cell(value: str) -> tuple[int, int] | None:
    text = value.strip()
    if not text:
        return None

    short_match = re.fullmatch(r"([A-Za-z]{3})/(\d{2})", text)
    if short_match:
        month = MONTH_ABBREV.get(short_match.group(1).lower())
        if month is not None:
            return _valid_month(2000 + int(short_match.group(2)), month)

    iso_match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})", text)
    if iso_match:
        year = int(iso_match.group(1))
        month = int(iso_match.group(2))
        return _valid_month(year, month)

    dotted_match = re.fullmatch(r"(\d{1,2})[-/.](\d{4})", text)
    if dotted_match:
        month = int(dotted_match.group(1))
        year = int(dotted_match.group(2))
        return _valid_month(year, month)

    words = re.split(r"\s+", text.lower())
    if len(words) >= 2:
        month_name = words[0]
        year_text = words[-1]
        if year_text.isdigit():
            month = BULGARIAN_MONTHS.get(month_name) or ENGLISH_MONTHS.get(month_name)
            if month is not None:
                return _valid_month(int(year_text), month)

    return None


def _detect_platform_columns(
    headers: list[str],
    mapping: ChannelReportMapping,
) -> dict[str, int]:
    detected: dict[str, int] = {}
    normalized_headers = [header.strip().casefold() for header in headers]
    for platform, aliases in mapping.platform_columns.items():
        alias_keys = {alias.strip().casefold() for alias in aliases}
        for index, header in enumerate(normalized_headers):
            if header in alias_keys:
                detected[platform] = index
                break
    return detected


def _valid_month(year: int, month: int) -> tuple[int, int] | None:
    if month < 1 or month > 12:
        return None
    return year, month


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _column_letters_to_index(value: str) -> int:
    text = value.strip().upper()
    if not text.isalpha():
        raise ChannelReportError(f"Invalid column label: {value!r}")
    index = 0
    for char in text:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _column_index_to_letters(index: int) -> str:
    value = index + 1
    label = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label
