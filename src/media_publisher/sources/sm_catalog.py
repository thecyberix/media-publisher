"""Write publishing dates back to the SM catalog Google Sheet.

The SM catalog's ``English`` tab has one ``<code>PubDate`` column per language
(``bgrPubDate`` for Bulgarian). Rows are keyed by ``ctLink``, the same link
that ingest stores in Airtable's ``Original Video`` field. After a video is
published on YouTube, its YouTube date is recorded in the target language's
column so the catalog reflects what has already gone out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from media_publisher.languages import LanguageDefinition
from media_publisher.sources.google_sheets import (
    GoogleSheetsClient,
    GoogleSheetsError,
    a1_cell,
    column_index_to_a1,
)

SM_CATALOG_TAB = "English"
SM_CATALOG_LINK_HEADER = "ctLink"
SM_CATALOG_HEADER_ROW = 1
SAFE_REQUEST_TIMEOUT_SECONDS = 30
SAFE_RETRY_ATTEMPTS = 2
# Google Sheets stores dates as days since this epoch.
_SHEET_DATE_EPOCH = date(1899, 12, 30)

STATUS_WRITTEN = "written"
STATUS_NOT_FOUND = "not_found"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_SKIPPED = "skipped"


@dataclass(frozen=True)
class SmCatalogDateResult:
    status: str
    message: str
    row: int | None = None

    @property
    def written(self) -> bool:
        return self.status == STATUS_WRITTEN


def sheet_date_serial(value: date) -> int:
    """Date as a Google Sheets serial number (keeps the cell's date format)."""
    return (value - _SHEET_DATE_EPOCH).days


def _sheet_prefix(sheet_title: str) -> str:
    escaped = sheet_title.replace("'", "''")
    return f"'{escaped}'"


def _header_index(headers: list[str], name: str) -> int | None:
    wanted = name.strip().casefold()
    for index, header in enumerate(headers):
        if header.strip().casefold() == wanted:
            return index
    return None


def record_publish_date(
    client: GoogleSheetsClient,
    spreadsheet_id: str,
    *,
    video_url: str | None,
    publish_date: date,
    date_header: str,
    sheet_title: str = SM_CATALOG_TAB,
) -> SmCatalogDateResult:
    """Set ``date_header`` on the catalog row for ``video_url``.

    An existing date is overwritten (the latest publish wins). A link that
    matches several rows is left alone rather than guessing which one to update.
    """
    from catalog_parser.airtable import normalize_original_video_key

    key = normalize_original_video_key(video_url)
    if key is None:
        return SmCatalogDateResult(
            STATUS_SKIPPED, "no YouTube/Instagram link on the record"
        )

    prefix = _sheet_prefix(sheet_title)
    header_rows = client.get_values(
        spreadsheet_id,
        f"{prefix}!{SM_CATALOG_HEADER_ROW}:{SM_CATALOG_HEADER_ROW}",
    )
    headers = header_rows[0] if header_rows else []
    link_column = _header_index(headers, SM_CATALOG_LINK_HEADER)
    date_column = _header_index(headers, date_header)
    if link_column is None:
        raise GoogleSheetsError(
            f"{SM_CATALOG_LINK_HEADER!r} column not found on tab {sheet_title!r}"
        )
    if date_column is None:
        raise GoogleSheetsError(
            f"{date_header!r} column not found on tab {sheet_title!r}"
        )

    link_letter = column_index_to_a1(link_column)
    links = client.get_values(
        spreadsheet_id, f"{prefix}!{link_letter}:{link_letter}"
    )
    matches = [
        row_number
        for row_number, cells in enumerate(links, start=1)
        if row_number > SM_CATALOG_HEADER_ROW
        and cells
        and normalize_original_video_key(cells[0]) == key
    ]
    if not matches:
        return SmCatalogDateResult(
            STATUS_NOT_FOUND, f"{key} is not in the {sheet_title!r} tab"
        )
    if len(matches) > 1:
        rows = ", ".join(str(row) for row in matches)
        return SmCatalogDateResult(
            STATUS_AMBIGUOUS, f"{key} matches several rows ({rows})"
        )

    row = matches[0]
    cell = a1_cell(sheet_title, row, date_column)
    existing = client.get_values(spreadsheet_id, cell)
    current = existing[0][0].strip() if existing and existing[0] else ""

    client.batch_update_values(
        spreadsheet_id,
        [(cell, [[sheet_date_serial(publish_date)]])],
    )
    message = f"row {row} {date_header} = {publish_date.isoformat()}"
    if current:
        message += f" (was {current})"
    return SmCatalogDateResult(STATUS_WRITTEN, message, row=row)


@dataclass(frozen=True)
class PlannedDate:
    url: str
    publish_date: date
    row: int | None
    status: str  # new | same | changed | not_found | ambiguous | skipped
    previous: date | None = None


@dataclass(frozen=True)
class PubDatePlan:
    sheet_title: str
    date_header: str
    date_column: int
    items: list[PlannedDate]

    def with_status(self, *statuses: str) -> list[PlannedDate]:
        return [item for item in self.items if item.status in statuses]

    def updates(self) -> list[tuple[str, list[list[object]]]]:
        return [
            (
                a1_cell(self.sheet_title, item.row, self.date_column),
                [[sheet_date_serial(item.publish_date)]],
            )
            for item in self.with_status("new", "changed")
            if item.row is not None
        ]


def _serial_to_date(value: str) -> date | None:
    try:
        return _SHEET_DATE_EPOCH + timedelta(days=int(float(value)))
    except (ValueError, OverflowError):
        return None


def plan_publish_dates(
    client: GoogleSheetsClient,
    spreadsheet_id: str,
    entries: list[tuple[str | None, date]],
    *,
    date_header: str,
    sheet_title: str = SM_CATALOG_TAB,
) -> PubDatePlan:
    """Work out catalog updates for many videos with three read requests."""
    from catalog_parser.airtable import normalize_original_video_key

    prefix = _sheet_prefix(sheet_title)
    header_rows = client.get_values(
        spreadsheet_id,
        f"{prefix}!{SM_CATALOG_HEADER_ROW}:{SM_CATALOG_HEADER_ROW}",
    )
    headers = header_rows[0] if header_rows else []
    link_column = _header_index(headers, SM_CATALOG_LINK_HEADER)
    date_column = _header_index(headers, date_header)
    if link_column is None or date_column is None:
        raise GoogleSheetsError(
            f"{SM_CATALOG_LINK_HEADER!r} / {date_header!r} column not found "
            f"on tab {sheet_title!r}"
        )
    link_letter = column_index_to_a1(link_column)
    date_letter = column_index_to_a1(date_column)
    links = client.get_values(
        spreadsheet_id, f"{prefix}!{link_letter}:{link_letter}"
    )
    current_dates = client.get_values(
        spreadsheet_id,
        f"{prefix}!{date_letter}:{date_letter}",
        value_render_option="UNFORMATTED_VALUE",
    )

    rows_by_key: dict[str, list[int]] = {}
    for row_number, cells in enumerate(links, start=1):
        if row_number <= SM_CATALOG_HEADER_ROW or not cells:
            continue
        key = normalize_original_video_key(cells[0])
        if key:
            rows_by_key.setdefault(key, []).append(row_number)

    items: list[PlannedDate] = []
    for url, publish_date in entries:
        key = normalize_original_video_key(url)
        if key is None:
            items.append(PlannedDate(url or "", publish_date, None, "skipped"))
            continue
        rows = rows_by_key.get(key, [])
        if not rows:
            items.append(PlannedDate(url or "", publish_date, None, "not_found"))
            continue
        if len(rows) > 1:
            items.append(PlannedDate(url or "", publish_date, None, "ambiguous"))
            continue
        row = rows[0]
        cells = current_dates[row - 1] if row - 1 < len(current_dates) else []
        raw = cells[0].strip() if cells else ""
        previous = _serial_to_date(raw) if raw else None
        if not raw:
            status = "new"
        elif previous == publish_date:
            status = "same"
        else:
            status = "changed"
        items.append(PlannedDate(url or "", publish_date, row, status, previous))
    return PubDatePlan(sheet_title, date_header, date_column, items)


def apply_plan(
    client: GoogleSheetsClient,
    spreadsheet_id: str,
    plan: PubDatePlan,
    *,
    chunk_size: int = 500,
) -> int:
    """Write every new/changed date in as few batchUpdate requests as possible."""
    updates = plan.updates()
    for start in range(0, len(updates), chunk_size):
        client.batch_update_values(spreadsheet_id, updates[start : start + chunk_size])
    return len(updates)


def try_record_publish_date(
    *,
    project_root: Path,
    service_account_path: Path | None,
    language: LanguageDefinition,
    video_url: str | None,
    publish_date: date,
    log: Callable[[str], None] = print,
) -> SmCatalogDateResult | None:
    """Best-effort wrapper: never raises, because the video is already published."""
    try:
        date_header = language.catalog_pub_date_header
        if date_header is None:
            return None
        if service_account_path is None or not service_account_path.exists():
            return None

        from catalog_parser.workflow.config import load_catalog_id

        # Short budget: a slow Sheets API must never hold up a publish run.
        client = GoogleSheetsClient.from_service_account(
            service_account_path,
            request_timeout=SAFE_REQUEST_TIMEOUT_SECONDS,
            retry_attempts=SAFE_RETRY_ATTEMPTS,
        )
        result = record_publish_date(
            client,
            load_catalog_id(project_root),
            video_url=video_url,
            publish_date=publish_date,
            date_header=date_header,
        )
    except Exception as exc:  # noqa: BLE001 — publishing already succeeded
        hint = ""
        if "protected cell" in str(exc).casefold():
            hint = (
                f" (the {language.catalog_pub_date_header} column is protected: add "
                "the Google service account as an editor of that protected range)"
            )
        elif "HTTP 403" in str(exc):
            hint = (
                " (share the SM catalog with the Google service account as Editor)"
            )
        # Sheets errors are multi-line JSON; keep the log to one readable line.
        reason = str(exc)
        if "protected cell" in reason.casefold():
            reason = "Google Sheets rejected the write"
        elif hint:
            reason = reason.splitlines()[0][:160]
        log(f"  SM catalog: publishing date not recorded: {reason}{hint}")
        return None

    if result.status != STATUS_SKIPPED:
        log(f"  SM catalog: {result.message}")
    return result
