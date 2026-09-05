from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from media_publisher.sources.google_sheets import (
    GoogleSheetsClient,
    GoogleSheetsError,
    SheetTab,
    format_sheet_tab_title,
)
from media_publisher.sources.quotes_config import QuotesSourcesConfig

SHEET_DATE_RE = re.compile(
    r"^(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})$"
)
SHEET_DATE_DASH_RE = re.compile(
    r"^(?P<day>\d{1,2})-(?P<month>[A-Za-z]+)-(?P<year>\d{2,4})$"
)

_MONTH_MAP = {
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


class QuotesSheetError(RuntimeError):
    pass


@dataclass(frozen=True)
class DailyQuoteText:
    day: int
    publish_date: date
    date_label: str
    text_bg: str
    text_en: str | None = None
    text_source: str = "ready"


def _quote_text_from_row(
    *,
    ready_text: str,
    edited_text: str,
    translation_text: str,
    require_ready: bool,
) -> tuple[str, str] | None:
    if ready_text:
        return ready_text, "ready"
    if require_ready:
        return None
    if edited_text:
        return edited_text, "edited"
    if translation_text:
        return translation_text, "translation"
    return None


def _column_index(headers: list[str], name: str) -> int | None:
    target = name.strip().casefold()
    for index, header in enumerate(headers):
        if header.strip().casefold() == target:
            return index
    return None


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def _resolve_year(raw: str) -> int:
    year = int(raw)
    if len(raw) == 2:
        # Quote archive dates are 2000+ (e.g. 12-Apr-24 → 2024).
        return 2000 + year
    return year


def parse_quote_sheet_date(value: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    match = SHEET_DATE_RE.match(text) or SHEET_DATE_DASH_RE.match(text)
    if match is None:
        return None
    month = _MONTH_MAP.get(match.group("month").casefold()[:3])
    if month is None:
        return None
    try:
        return date(_resolve_year(match.group("year")), month, int(match.group("day")))
    except ValueError:
        return None


def resolve_month_quote_tab(
    client: GoogleSheetsClient,
    spreadsheet_id: str,
    *,
    year: int,
    month: int,
) -> SheetTab:
    """Resolve a month tab, accepting both 'Jul 2024' and 'July 2024' titles."""
    candidates = [
        format_sheet_tab_title(year, month),
        f"{calendar.month_name[month]} {year}",
    ]
    tabs = client.list_tabs(spreadsheet_id)
    by_title = {tab.title.casefold().strip(): tab for tab in tabs}
    for name in candidates:
        match = by_title.get(name.casefold().strip())
        if match is not None:
            return match
    return client.resolve_sheet_tab_for_month(
        spreadsheet_id,
        year=year,
        month=month,
    )


def load_monthly_quote_texts(
    client: GoogleSheetsClient,
    config: QuotesSourcesConfig,
    *,
    year: int,
    month: int,
    require_ready: bool = True,
    spreadsheet_id: str | None = None,
) -> list[DailyQuoteText]:
    """Load quote rows for render/publish/Drive.

    ``require_ready=True`` (Drive dump) uses only Ready. Scheduling/publishing
    passes ``require_ready=False`` so Edited, then Translation, can substitute.
    """
    sheet_config = config.quotes_sheet
    resolved_id = spreadsheet_id
    if not resolved_id:
        raise QuotesSheetError(
            "spreadsheet_id is required "
            "(resolve the Bulgarian year workbook under DRIVE_URL/Quotes)"
        )
    tab = resolve_month_quote_tab(
        client,
        resolved_id,
        year=year,
        month=month,
    )
    escaped = tab.title.replace("'", "''")
    rows = client.get_values(resolved_id, f"'{escaped}'!A:Z")
    if not rows:
        return []

    headers = rows[0]
    date_index = _column_index(headers, str(sheet_config.get("date_column", "Date")))
    english_index = _column_index(
        headers,
        str(sheet_config.get("text_en_column", "English")),
    )
    ready_index = _column_index(
        headers,
        str(sheet_config.get("ready_column", "Ready")),
    )
    edited_index = _column_index(
        headers,
        str(sheet_config.get("edited_column", "Edited")),
    )
    translation_index = _column_index(
        headers,
        str(sheet_config.get("text_bg_column", "Translation")),
    )
    if date_index is None:
        raise QuotesSheetError("Quotes sheet is missing a Date column")
    if require_ready and ready_index is None:
        raise QuotesSheetError("Quotes sheet is missing a Ready column")
    if (
        not require_ready
        and ready_index is None
        and edited_index is None
        and translation_index is None
    ):
        raise QuotesSheetError(
            "Quotes sheet is missing Ready, Edited, and Translation columns"
        )

    quotes: list[DailyQuoteText] = []

    for row in rows[1:]:
        date_label = _cell(row, date_index)
        if not date_label:
            continue
        publish_date = parse_quote_sheet_date(date_label)
        if publish_date is None:
            continue
        if publish_date.year != year or publish_date.month != month:
            continue

        selected = _quote_text_from_row(
            ready_text=_cell(row, ready_index),
            edited_text=_cell(row, edited_index),
            translation_text=_cell(row, translation_index),
            require_ready=require_ready,
        )
        if selected is None:
            continue
        text_bg, text_source = selected

        quotes.append(
            DailyQuoteText(
                day=publish_date.day,
                publish_date=publish_date,
                date_label=date_label,
                text_bg=text_bg,
                text_en=_cell(row, english_index) or None,
                text_source=text_source,
            )
        )

    return sorted(quotes, key=lambda quote: quote.day)
