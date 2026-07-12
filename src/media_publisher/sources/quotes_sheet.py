from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from media_publisher.sources.google_sheets import GoogleSheetsClient, GoogleSheetsError
from media_publisher.sources.quotes_config import QuotesSourcesConfig

SHEET_DATE_RE = re.compile(
    r"^(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})$"
)


class QuotesSheetError(RuntimeError):
    pass


@dataclass(frozen=True)
class DailyQuoteText:
    day: int
    publish_date: date
    date_label: str
    text_bg: str
    text_en: str | None = None


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


def parse_quote_sheet_date(value: str) -> date | None:
    match = SHEET_DATE_RE.match(value.strip())
    if match is None:
        return None
    month_name = match.group("month").casefold()
    month_map = {
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
    month_key = month_name[:3]
    month = month_map.get(month_key)
    if month is None:
        return None
    return date(int(match.group("year")), month, int(match.group("day")))


def load_monthly_quote_texts(
    client: GoogleSheetsClient,
    config: QuotesSourcesConfig,
    *,
    year: int,
    month: int,
) -> list[DailyQuoteText]:
    sheet_config = config.quotes_sheet
    tab = client.resolve_sheet_tab_for_month(
        config.spreadsheet_id,
        year=year,
        month=month,
    )
    escaped = tab.title.replace("'", "''")
    rows = client.get_values(config.spreadsheet_id, f"'{escaped}'!A:Z")
    if not rows:
        return []

    headers = rows[0]
    date_index = _column_index(headers, str(sheet_config.get("date_column", "Date")))
    translation_index = _column_index(
        headers,
        str(sheet_config.get("text_bg_column", "Translation")),
    )
    english_index = _column_index(
        headers,
        str(sheet_config.get("text_en_column", "English")),
    )
    ready_index = _column_index(
        headers,
        str(sheet_config.get("ready_column", "Ready")),
    )
    if date_index is None:
        raise QuotesSheetError("Quotes sheet is missing a Date column")
    if translation_index is None:
        raise QuotesSheetError("Quotes sheet is missing a Translation column")

    prefer_ready = bool(sheet_config.get("prefer_ready_text", True))
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

        ready_text = _cell(row, ready_index)
        translation_text = _cell(row, translation_index)
        if prefer_ready and ready_text:
            text_bg = ready_text
        else:
            text_bg = translation_text
        if not text_bg:
            continue

        quotes.append(
            DailyQuoteText(
                day=publish_date.day,
                publish_date=publish_date,
                date_label=date_label,
                text_bg=text_bg,
                text_en=_cell(row, english_index) or None,
            )
        )

    return sorted(quotes, key=lambda quote: quote.day)
