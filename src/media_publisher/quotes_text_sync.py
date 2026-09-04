"""Sync English quotes into Bulgarian month sheets and prepare translations."""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from media_publisher.sources.drive_layout import resolve_quotes_folder_id
from media_publisher.sources.google_drive import (
    GoogleDriveClient,
    GoogleDriveError,
    format_month_folder_name,
)
from media_publisher.sources.google_sheets import (
    GoogleSheetsClient,
    GoogleSheetsError,
    SheetTab,
    a1_cell,
    format_sheet_tab_title,
)
from media_publisher.sources.quotes_config import QuotesConfigError, QuotesSourcesConfig
from media_publisher.sources.quotes_sheet import parse_quote_sheet_date

PrintFn = Callable[[str], None]

DEFAULT_DEST_HEADERS = ("Date", "English", "Translation", "Edited", "Ready")
DEFAULT_ENGLISH_COLUMNS = ("English", "Quote", "Quotes")
# Older month tabs use Proofread instead of Ready; some use trailing spaces.
DEFAULT_READY_COLUMN_CANDIDATES = ("Ready", "Proofread")


def current_and_next_months(reference: date) -> list[tuple[int, int]]:
    year = reference.year
    month = reference.month
    if month == 12:
        return [(year, month), (year + 1, 1)]
    return [(year, month), (year, month + 1)]


class QuotesTextSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class EnglishQuoteRow:
    day: int
    publish_date: date
    date_label: str
    english: str
    previously_posted_on: date | None
    previously_posted_label: str


@dataclass(frozen=True)
class DestinationSheetRef:
    spreadsheet_id: str
    spreadsheet_name: str
    tab: SheetTab
    created: bool = False


@dataclass(frozen=True)
class QuoteTextChange:
    action: str  # added | updated | reused | translated
    year: int
    month: int
    day: int
    date_label: str
    detail: str


@dataclass
class QuotesTextSyncResult:
    changes: list[QuoteTextChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def added_count(self) -> int:
        return sum(1 for item in self.changes if item.action == "added")

    @property
    def updated_count(self) -> int:
        return sum(1 for item in self.changes if item.action == "updated")

    @property
    def translated_count(self) -> int:
        return sum(1 for item in self.changes if item.action == "translated")

    @property
    def reused_count(self) -> int:
        return sum(1 for item in self.changes if item.action == "reused")


def _column_index(headers: list[str], *names: str) -> int | None:
    targets = {name.strip().casefold() for name in names if name and name.strip()}
    for index, header in enumerate(headers):
        if header.strip().casefold() in targets:
            return index
    return None


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def _normalize_text(value: str) -> str:
    return " ".join((value or "").split())


def _unique_names(*names: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        text = (name or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _format_year_workbook_pattern(year: int, pattern: str) -> str:
    return pattern.format(year=year)


def bulgarian_year_workbook_name(
    year: int, config: QuotesSourcesConfig | None = None
) -> str:
    pattern = "Sadhguru Quotes Bulgarian {year}"
    if config is not None:
        raw = config.translated_quotes_drive.get("year_workbook_pattern")
        if isinstance(raw, str) and raw.strip():
            pattern = raw.strip()
    return _format_year_workbook_pattern(year, pattern)


def workbook_name_stems(file_name: str) -> set[str]:
    """Normalize Drive file names for matching (ignore .xlsx and similar)."""
    text = (file_name or "").strip()
    stems = {text.casefold()}
    lowered = text.casefold()
    for ext in (".xlsx", ".xls", ".gsheet"):
        if lowered.endswith(ext):
            stems.add(lowered[: -len(ext)].strip())
    return stems


def matches_year_workbook_name(file_name: str, expected_name: str) -> bool:
    expected = expected_name.casefold().strip()
    return expected in workbook_name_stems(file_name)


def year_workbook_name(year: int, config: QuotesSourcesConfig | None = None) -> str:
    """Alias for bulgarian_year_workbook_name (back-compat)."""
    return bulgarian_year_workbook_name(year, config)


def month_tab_name_candidates(year: int, month: int) -> list[str]:
    """Month tab titles used across BG year workbooks (abbr and full month)."""
    return _unique_names(
        format_sheet_tab_title(year, month),
        f"{calendar.month_name[month]} {year}",
        format_month_folder_name(
            "{month:02d} {month_abbr} {year}",
            year=year,
            month=month,
        ),
    )


def month_file_name_candidates(year: int, month: int) -> list[str]:
    """Deprecated alias kept for tests; prefer month_tab_name_candidates."""
    return month_tab_name_candidates(year, month)


def find_tab_by_candidates(
    tabs: list[SheetTab],
    candidates: list[str],
) -> SheetTab | None:
    by_title = {tab.title.casefold().strip(): tab for tab in tabs}
    for name in candidates:
        match = by_title.get(name.casefold().strip())
        if match is not None:
            return match
    return None


def _destination_headers(config: QuotesSourcesConfig) -> list[str]:
    dest_cfg = config.translated_quotes_drive
    headers = dest_cfg.get("headers") or list(DEFAULT_DEST_HEADERS)
    if not isinstance(headers, list):
        headers = list(DEFAULT_DEST_HEADERS)
    return [str(item) for item in headers]


def _ensure_month_tab(
    sheets: GoogleSheetsClient,
    *,
    spreadsheet_id: str,
    year: int,
    month: int,
    create_if_missing: bool,
) -> SheetTab | None:
    tabs = sheets.list_tabs(spreadsheet_id)
    candidates = month_tab_name_candidates(year, month)
    existing = find_tab_by_candidates(tabs, candidates)
    if existing is not None:
        return existing
    if not create_if_missing:
        return None

    preferred_title = format_sheet_tab_title(year, month)
    response = sheets.batch_update_spreadsheet(
        spreadsheet_id,
        [
            {
                "addSheet": {
                    "properties": {
                        "title": preferred_title,
                    }
                }
            }
        ],
    )
    replies = response.get("replies") if isinstance(response, dict) else None
    sheet_id = None
    if isinstance(replies, list) and replies:
        props = (
            (replies[0] or {}).get("addSheet", {}).get("properties", {})
            if isinstance(replies[0], dict)
            else {}
        )
        if isinstance(props, dict) and isinstance(props.get("sheetId"), int):
            sheet_id = props["sheetId"]
    if sheet_id is None:
        tabs = sheets.list_tabs(spreadsheet_id)
        created = find_tab_by_candidates(tabs, [preferred_title])
        if created is None:
            raise QuotesTextSyncError(
                f"Failed to create month tab {preferred_title!r} in {spreadsheet_id}"
            )
        return created
    return SheetTab(sheet_id=sheet_id, title=preferred_title)


def _read_sheet_rows(
    sheets: GoogleSheetsClient,
    spreadsheet_id: str,
    tab_title: str,
) -> list[list[str]]:
    escaped = tab_title.replace("'", "''")
    return sheets.get_values(spreadsheet_id, f"'{escaped}'!A:Z")


def load_english_quote_rows(
    sheets: GoogleSheetsClient,
    config: QuotesSourcesConfig,
    *,
    year: int,
    month: int,
) -> list[EnglishQuoteRow]:
    english_cfg = config.english_quotes
    spreadsheet_id = config.english_spreadsheet_id
    tabs = sheets.list_tabs(spreadsheet_id)
    tab = find_tab_by_candidates(tabs, month_tab_name_candidates(year, month))
    if tab is None:
        tab = sheets.resolve_sheet_tab_for_month(spreadsheet_id, year=year, month=month)
    rows = _read_sheet_rows(sheets, spreadsheet_id, tab.title)
    if not rows:
        return []

    headers = rows[0]
    date_index = _column_index(
        headers,
        str(english_cfg.get("date_column", "Date")),
    )
    english_names = english_cfg.get("english_columns") or list(DEFAULT_ENGLISH_COLUMNS)
    if isinstance(english_names, str):
        english_names = [english_names]
    english_index = _column_index(headers, *[str(name) for name in english_names])
    previous_index = _column_index(
        headers,
        str(english_cfg.get("previously_posted_column", "Previously Posted on")),
        "Previously posted on",
        "Previously Posted",
    )
    if date_index is None:
        raise QuotesTextSyncError(
            f"English quotes sheet {tab.title!r} is missing a Date column"
        )
    if english_index is None:
        raise QuotesTextSyncError(
            f"English quotes sheet {tab.title!r} is missing an English/Quote column"
        )

    quotes: list[EnglishQuoteRow] = []
    for row in rows[1:]:
        date_label = _cell(row, date_index)
        if not date_label:
            continue
        publish_date = parse_quote_sheet_date(date_label)
        if publish_date is None:
            continue
        if publish_date.year != year or publish_date.month != month:
            continue
        english = _cell(row, english_index)
        if not english:
            continue
        previous_label = _cell(row, previous_index)
        previous_date = (
            parse_quote_sheet_date(previous_label) if previous_label else None
        )
        quotes.append(
            EnglishQuoteRow(
                day=publish_date.day,
                publish_date=publish_date,
                date_label=date_label,
                english=english,
                previously_posted_on=previous_date,
                previously_posted_label=previous_label,
            )
        )
    return sorted(quotes, key=lambda item: item.day)


def resolve_year_workbook(
    *,
    drive: GoogleDriveClient,
    config: QuotesSourcesConfig,
    year: int,
    create_if_missing: bool = True,
) -> tuple[str, str, bool] | None:
    """
    Return (spreadsheet_id, spreadsheet_name, created) for a Bulgarian year workbook
    discovered by name pattern under DRIVE_URL/Quotes.
    """
    folder_id = resolve_quotes_folder_id(drive)
    expected_name = bulgarian_year_workbook_name(year, config)
    spreadsheets = drive.list_spreadsheets(folder_id)
    for item in spreadsheets:
        if matches_year_workbook_name(item.name, expected_name):
            return item.id, item.name, False

    if not create_if_missing:
        return None

    created = drive.create_google_spreadsheet(folder_id, expected_name)
    return created.id, created.name, True


def resolve_bulgarian_spreadsheet_id(
    *,
    drive: GoogleDriveClient,
    config: QuotesSourcesConfig,
    year: int,
) -> str:
    """Resolve the BG year workbook id from DRIVE_URL/Quotes (no hard-coded year URL)."""
    workbook = resolve_year_workbook(
        drive=drive,
        config=config,
        year=year,
        create_if_missing=False,
    )
    if workbook is not None:
        return workbook[0]
    raise QuotesTextSyncError(
        f"Bulgarian quotes workbook {bulgarian_year_workbook_name(year, config)!r} "
        "was not found under DRIVE_URL/Quotes"
    )


def resolve_destination_month_sheet(
    *,
    drive: GoogleDriveClient,
    sheets: GoogleSheetsClient,
    config: QuotesSourcesConfig,
    year: int,
    month: int,
    create_if_missing: bool = True,
) -> DestinationSheetRef | None:
    """
    Resolve the Bulgarian month tab inside the yearly workbook:

      Sadhguru Quotes Bulgarian {year}  →  month tab (abbr or full month name)
    """
    workbook = resolve_year_workbook(
        drive=drive,
        config=config,
        year=year,
        create_if_missing=create_if_missing,
    )
    if workbook is None:
        return None

    spreadsheet_id, spreadsheet_name, workbook_created = workbook
    tab = _ensure_month_tab(
        sheets,
        spreadsheet_id=spreadsheet_id,
        year=year,
        month=month,
        create_if_missing=create_if_missing,
    )
    if tab is None:
        return None

    created = workbook_created
    rows = _read_sheet_rows(sheets, spreadsheet_id, tab.title)
    if not rows or not any(cell.strip() for cell in rows[0]):
        headers = _destination_headers(config)
        escaped = tab.title.replace("'", "''")
        sheets.batch_update_values(
            spreadsheet_id,
            [(f"'{escaped}'!A1", [headers])],
        )
        created = True

    return DestinationSheetRef(
        spreadsheet_id=spreadsheet_id,
        spreadsheet_name=spreadsheet_name,
        tab=tab,
        created=created,
    )


def _ready_column_candidates(config: QuotesSourcesConfig) -> list[str]:
    dest_cfg = config.translated_quotes_drive
    sheet_cfg = config.quotes_sheet
    configured: list[str] = []
    for raw in (
        dest_cfg.get("ready_column_candidates"),
        dest_cfg.get("ready_column"),
        sheet_cfg.get("ready_column"),
    ):
        if isinstance(raw, str) and raw.strip():
            configured.append(raw)
        elif isinstance(raw, list):
            configured.extend(str(item) for item in raw if str(item).strip())
    return _unique_names(*configured, *DEFAULT_READY_COLUMN_CANDIDATES)


def find_ready_column_index(headers: list[str], config: QuotesSourcesConfig) -> int | None:
    """Locate the approved-text column regardless of sheet layout / column order."""
    return _column_index(headers, *_ready_column_candidates(config))


def extract_ready_text_from_row(
    row: list[str],
    headers: list[str],
    config: QuotesSourcesConfig,
) -> str | None:
    """
    Read approved Bulgarian text from a historical row.

    Layouts vary by year/month:
    - modern: Ready / Ready  (trailing space)
    - mid-2023: Proofread (same role as Ready)
    - older: Ready among translator-name columns

    No fallback to Edited/Translation — callers use AI translation instead.
    """
    ready_index = find_ready_column_index(headers, config)
    if ready_index is None:
        return None
    ready = _cell(row, ready_index)
    return ready or None


def lookup_ready_translation(
    *,
    drive: GoogleDriveClient,
    sheets: GoogleSheetsClient,
    config: QuotesSourcesConfig,
    posted_on: date,
) -> str | None:
    dest = resolve_destination_month_sheet(
        drive=drive,
        sheets=sheets,
        config=config,
        year=posted_on.year,
        month=posted_on.month,
        create_if_missing=False,
    )
    if dest is None:
        return None

    dest_cfg = config.translated_quotes_drive
    sheet_cfg = config.quotes_sheet
    rows = _read_sheet_rows(sheets, dest.spreadsheet_id, dest.tab.title)
    if not rows:
        return None
    headers = rows[0]
    date_index = _column_index(
        headers,
        str(dest_cfg.get("date_column") or sheet_cfg.get("date_column", "Date")),
    )
    if date_index is None:
        return None

    for row in rows[1:]:
        label = _cell(row, date_index)
        row_date = parse_quote_sheet_date(label) if label else None
        if row_date != posted_on:
            continue
        return extract_ready_text_from_row(row, headers, config)
    return None


def translate_quote_text(
    english: str,
    *,
    project_root: Path | None = None,
) -> str:
    """Translate a daily quote with the shared translation provider."""
    from catalog_parser.translation.prefill import ai_prefill_enabled
    from catalog_parser.translation.rag_translate import (
        chat_completion,
        chat_config_from_env,
        translation_provider_disabled,
    )

    text = (english or "").strip()
    if not text:
        raise QuotesTextSyncError("Cannot translate empty quote text")
    if translation_provider_disabled() or not ai_prefill_enabled():
        raise QuotesTextSyncError(
            "AI translation is disabled "
            "(set TRANSLATION_PROVIDER=anthropic|openai and TRANSLATION_API_KEY)"
        )

    from media_publisher.languages import selected_language

    language = selected_language()
    ingest = language.require_ingest()
    name = language.name
    system = (
        f"You are a professional translator for Sadhguru daily quotes into {name}. "
        "Preserve meaning and spiritual tone. Keep the quote concise and natural. "
        f"Use {name} quotation marks {ingest.quote_open}…{ingest.quote_close} "
        "when the English uses quotation marks. "
        "Do not add attribution, explanations, or hashtags. "
        f"Return only the {name} quote text."
    )
    user = f"Translate this daily quote into {name}:\n\n{text}"

    # Prefer RAG title examples when the corpus is available.
    try:
        from catalog_parser.translation.index import (
            DEFAULT_HOLDOUT_PATH,
            DEFAULT_METADATA_PAIRS_PATH,
            DEFAULT_METADATA_TITLE_INDEX_PATH,
            load_or_build_metadata_index,
        )
        from catalog_parser.translation.rag_translate import translate_metadata_field

        root = project_root or Path.cwd()
        pairs = root / DEFAULT_METADATA_PAIRS_PATH
        if pairs.is_file():
            index = load_or_build_metadata_index(
                "title",
                index_path=root / DEFAULT_METADATA_TITLE_INDEX_PATH,
                pairs_path=pairs,
                holdout_path=root / DEFAULT_HOLDOUT_PATH,
            )
            chat = chat_config_from_env()
            return translate_metadata_field(
                text,
                kind="caption",
                index=index,
                config=chat,
            ).strip()
    except Exception:
        pass

    chat = chat_config_from_env()
    translated = chat_completion(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        chat,
    ).strip()
    if not translated:
        raise QuotesTextSyncError("Empty quote translation returned by model")
    return translated


def _destination_column_map(
    headers: list[str],
    config: QuotesSourcesConfig,
) -> dict[str, int]:
    dest_cfg = config.translated_quotes_drive
    sheet_cfg = config.quotes_sheet
    date_index = _column_index(
        headers,
        str(dest_cfg.get("date_column") or sheet_cfg.get("date_column", "Date")),
    )
    english_index = _column_index(
        headers,
        str(dest_cfg.get("english_column") or sheet_cfg.get("text_en_column", "English")),
        "English",
        "Quote",
    )
    translation_index = _column_index(
        headers,
        str(
            dest_cfg.get("translation_column")
            or sheet_cfg.get("text_bg_column", "Translation")
        ),
        "Translation",
    )
    edited_index = _column_index(
        headers,
        str(dest_cfg.get("edited_column", "Edited")),
        "Edited",
    )
    ready_index = _column_index(
        headers,
        str(dest_cfg.get("ready_column") or sheet_cfg.get("ready_column", "Ready")),
        "Ready",
    )
    missing = [
        name
        for name, index in (
            ("Date", date_index),
            ("English", english_index),
            ("Translation", translation_index),
        )
        if index is None
    ]
    if missing:
        raise QuotesTextSyncError(
            "Destination sheet is missing required column(s): " + ", ".join(missing)
        )
    assert date_index is not None
    assert english_index is not None
    assert translation_index is not None
    return {
        "date": date_index,
        "english": english_index,
        "translation": translation_index,
        "edited": edited_index if edited_index is not None else -1,
        "ready": ready_index if ready_index is not None else -1,
    }


def sync_month_quote_texts(
    *,
    config: QuotesSourcesConfig,
    sheets: GoogleSheetsClient,
    drive: GoogleDriveClient,
    year: int,
    month: int,
    project_root: Path | None = None,
    print_line: PrintFn | None = None,
    translate_fn: Callable[[str], str] | None = None,
) -> QuotesTextSyncResult:
    log = print_line or (lambda _msg: None)
    result = QuotesTextSyncResult()

    try:
        english_rows = load_english_quote_rows(
            sheets, config, year=year, month=month
        )
    except (QuotesTextSyncError, QuotesConfigError, GoogleSheetsError) as exc:
        result.warnings.append(f"{year}-{month:02d}: {exc}")
        return result

    if not english_rows:
        log(f"{year}-{month:02d}: no English quotes found")
        return result

    try:
        dest = resolve_destination_month_sheet(
            drive=drive,
            sheets=sheets,
            config=config,
            year=year,
            month=month,
            create_if_missing=True,
        )
    except (QuotesTextSyncError, QuotesConfigError, GoogleDriveError, GoogleSheetsError) as exc:
        result.warnings.append(f"{year}-{month:02d}: destination resolve failed: {exc}")
        return result

    assert dest is not None
    if dest.created:
        log(f"{year}-{month:02d}: created destination spreadsheet {dest.spreadsheet_name!r}")

    rows = _read_sheet_rows(sheets, dest.spreadsheet_id, dest.tab.title)
    if not rows:
        dest_cfg = config.translated_quotes_drive
        headers = [str(h) for h in (dest_cfg.get("headers") or list(DEFAULT_DEST_HEADERS))]
        escaped = dest.tab.title.replace("'", "''")
        sheets.batch_update_values(
            dest.spreadsheet_id,
            [(f"'{escaped}'!A1", [headers])],
        )
        rows = [headers]

    headers = rows[0]
    columns = _destination_column_map(headers, config)
    # Ensure Edited column exists when reuse is needed.
    if columns["edited"] < 0:
        headers = list(headers) + ["Edited"]
        columns["edited"] = len(headers) - 1
        escaped = dest.tab.title.replace("'", "''")
        sheets.batch_update_values(
            dest.spreadsheet_id,
            [(f"'{escaped}'!A1", [headers])],
        )

    existing_by_day: dict[int, tuple[int, list[str]]] = {}
    for row_number, row in enumerate(rows[1:], start=2):
        label = _cell(row, columns["date"])
        row_date = parse_quote_sheet_date(label) if label else None
        if row_date is None:
            continue
        if row_date.year != year or row_date.month != month:
            continue
        existing_by_day[row_date.day] = (row_number, row)

    updates: list[tuple[str, list[list[Any]]]] = []
    next_append_row = len(rows) + 1
    translator = translate_fn or (
        lambda text: translate_quote_text(text, project_root=project_root)
    )

    for quote in english_rows:
        existing = existing_by_day.get(quote.day)
        existing_english = _cell(existing[1], columns["english"]) if existing else ""
        if existing and _normalize_text(existing_english) == _normalize_text(quote.english):
            continue

        action = "updated" if existing else "added"
        if existing is None:
            row_number = next_append_row
            next_append_row += 1
            prior_row: list[str] = []
        else:
            row_number = existing[0]
            prior_row = existing[1]

        width = max(len(headers), columns["edited"] + 1, columns["translation"] + 1)
        new_row = [""] * width
        for index, value in enumerate(prior_row):
            if index < width:
                new_row[index] = value
        new_row[columns["date"]] = quote.date_label
        new_row[columns["english"]] = quote.english

        translation_detail = ""
        reuse_text: str | None = None
        if quote.previously_posted_on is not None:
            reuse_text = lookup_ready_translation(
                drive=drive,
                sheets=sheets,
                config=config,
                posted_on=quote.previously_posted_on,
            )
            if reuse_text:
                new_row[columns["edited"]] = reuse_text
                translation_detail = (
                    f"reused Ready from {quote.previously_posted_label}"
                )
                result.changes.append(
                    QuoteTextChange(
                        action="reused",
                        year=year,
                        month=month,
                        day=quote.day,
                        date_label=quote.date_label,
                        detail=translation_detail,
                    )
                )
            else:
                result.warnings.append(
                    f"{quote.date_label}: Previously Posted on "
                    f"{quote.previously_posted_label!r} but Ready text not found; "
                    "falling back to AI translation"
                )

        if not reuse_text:
            try:
                translated = translator(quote.english)
            except Exception as exc:  # noqa: BLE001 — continue other days
                result.warnings.append(
                    f"{quote.date_label}: AI translation failed: {exc}"
                )
                translated = ""
            if translated:
                new_row[columns["translation"]] = translated
                translation_detail = "AI translation"
                result.changes.append(
                    QuoteTextChange(
                        action="translated",
                        year=year,
                        month=month,
                        day=quote.day,
                        date_label=quote.date_label,
                        detail=translation_detail,
                    )
                )

        for column_key, column_index in (
            ("date", columns["date"]),
            ("english", columns["english"]),
            ("translation", columns["translation"]),
            ("edited", columns["edited"]),
        ):
            if column_index < 0:
                continue
            if column_key == "edited" and not reuse_text:
                continue
            if column_key == "translation" and (reuse_text or not new_row[column_index]):
                continue
            updates.append(
                (
                    a1_cell(dest.tab.title, row_number, column_index),
                    [[new_row[column_index]]],
                )
            )

        existing_by_day[quote.day] = (row_number, new_row)
        result.changes.append(
            QuoteTextChange(
                action=action,
                year=year,
                month=month,
                day=quote.day,
                date_label=quote.date_label,
                detail=translation_detail or "english only",
            )
        )
        log(
            f"{year}-{month:02d}-{quote.day:02d}: {action}"
            + (f" ({translation_detail})" if translation_detail else "")
        )

    if updates:
        sheets.batch_update_values(dest.spreadsheet_id, updates)

    return result


def sync_quote_texts_for_months(
    *,
    config: QuotesSourcesConfig,
    sheets: GoogleSheetsClient,
    drive: GoogleDriveClient,
    reference_date: date,
    project_root: Path | None = None,
    print_line: PrintFn | None = None,
    translate_fn: Callable[[str], str] | None = None,
    months: list[tuple[int, int]] | None = None,
) -> QuotesTextSyncResult:
    targets = months or current_and_next_months(reference_date)
    combined = QuotesTextSyncResult()
    for year, month in targets:
        month_result = sync_month_quote_texts(
            config=config,
            sheets=sheets,
            drive=drive,
            year=year,
            month=month,
            project_root=project_root,
            print_line=print_line,
            translate_fn=translate_fn,
        )
        combined.changes.extend(month_result.changes)
        combined.warnings.extend(month_result.warnings)
    return combined
