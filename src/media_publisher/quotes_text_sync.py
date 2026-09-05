"""Sync English quotes into Bulgarian month sheets and prepare translations."""
from __future__ import annotations

import calendar
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from media_publisher.sources.drive_layout import resolve_quotes_folder_id
from media_publisher.sources.google_drive import (
    GOOGLE_SHEETS_MIME_TYPE,
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

DEFAULT_DEST_HEADERS = ("Date", "English", "Translation", "Edited", "Ready", "Comment")
DEFAULT_ENGLISH_COLUMNS = ("English", "Quote", "Quotes")
# Older month tabs use Proofread instead of Ready; some use trailing spaces.
DEFAULT_READY_COLUMN_CANDIDATES = ("Ready", "Proofread")
QUOTES_READY_INDEX_RELATIVE = "data/quotes_ready_index.json"
READY_INDEX_VERSION = 1
DEFAULT_QUOTE_RAG_TOP_K = 8
STALE_READY_BACKGROUND = {"red": 1.0, "green": 1.0, "blue": 0.0}


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
class ReadyQuoteMatch:
    ready: str
    spreadsheet_name: str
    tab_title: str
    date_label: str = ""
    english: str = ""


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


_ENGLISH_PUNCT_TRANSLATE = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
    }
)


def _normalize_english(value: str) -> str:
    """Collapse whitespace, normalize quotes/dashes, and ignore case for archive matching."""
    return _normalize_text((value or "").translate(_ENGLISH_PUNCT_TRANSLATE)).casefold()


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


def _year_from_bulgarian_workbook_name(
    file_name: str, config: QuotesSourcesConfig | None = None
) -> int | None:
    pattern = "Sadhguru Quotes Bulgarian {year}"
    if config is not None:
        raw = config.translated_quotes_drive.get("year_workbook_pattern")
        if isinstance(raw, str) and raw.strip():
            pattern = raw.strip()
    prefix, separator, suffix = pattern.partition("{year}")
    if not separator:
        return None
    regex = re.compile(
        r"^"
        + re.escape(prefix.strip())
        + r"\s+(\d{4})"
        + (r"\s+" + re.escape(suffix.strip()) if suffix.strip() else "")
        + r"$",
        re.IGNORECASE,
    )
    for stem in workbook_name_stems(file_name):
        match = regex.match(stem.strip())
        if match:
            return int(match.group(1))
    return None


def list_bulgarian_year_workbooks(
    drive: GoogleDriveClient,
    config: QuotesSourcesConfig,
) -> list[tuple[str, str, int]]:
    """Google Sheets year workbooks under DRIVE_URL/Quotes, newest year first."""
    folder_id = resolve_quotes_folder_id(drive)
    found: list[tuple[str, str, int]] = []
    for item in drive.list_spreadsheets(folder_id):
        if item.mime_type != GOOGLE_SHEETS_MIME_TYPE:
            continue
        year = _year_from_bulgarian_workbook_name(item.name, config)
        if year is None:
            continue
        found.append((item.id, item.name, year))
    found.sort(key=lambda item: item[2], reverse=True)
    return found


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


def _sheet_a1_range(tab_title: str, cells: str = "A:Z") -> str:
    escaped = tab_title.replace("'", "''")
    return f"'{escaped}'!{cells}"


def _read_sheet_rows(
    sheets: GoogleSheetsClient,
    spreadsheet_id: str,
    tab_title: str,
) -> list[list[str]]:
    return sheets.get_values(spreadsheet_id, _sheet_a1_range(tab_title))


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


def _english_column_candidates(config: QuotesSourcesConfig) -> list[str]:
    dest_cfg = config.translated_quotes_drive
    sheet_cfg = config.quotes_sheet
    return _unique_names(
        str(dest_cfg.get("english_column") or ""),
        str(sheet_cfg.get("text_en_column") or ""),
        *DEFAULT_ENGLISH_COLUMNS,
    )


def lookup_ready_by_english(
    english: str,
    ready_by_english: Mapping[str, ReadyQuoteMatch],
) -> ReadyQuoteMatch | None:
    key = _normalize_english(english)
    if not key:
        return None
    return ready_by_english.get(key)


def quotes_ready_index_path(project_root: Path | None = None) -> Path:
    root = project_root or Path.cwd()
    return root / QUOTES_READY_INDEX_RELATIVE


def _ready_index_quotes_payload(
    index: Mapping[str, ReadyQuoteMatch],
) -> list[dict[str, str]]:
    quotes = [
        {
            "english_key": key,
            "english": match.english,
            "ready": match.ready,
            "spreadsheet_name": match.spreadsheet_name,
            "tab_title": match.tab_title,
            "date_label": match.date_label,
        }
        for key, match in index.items()
    ]
    quotes.sort(key=lambda item: item["english_key"])
    return quotes


def load_ready_index_file(path: Path) -> dict[str, ReadyQuoteMatch]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    quotes = payload.get("quotes")
    if not isinstance(quotes, list):
        return {}
    index: dict[str, ReadyQuoteMatch] = {}
    for item in quotes:
        if not isinstance(item, dict):
            continue
        english = str(item.get("english") or "")
        key = str(item.get("english_key") or "").strip() or _normalize_english(english)
        ready = str(item.get("ready") or "").strip()
        if not key or not ready:
            continue
        index[key] = ReadyQuoteMatch(
            ready=ready,
            spreadsheet_name=str(item.get("spreadsheet_name") or ""),
            tab_title=str(item.get("tab_title") or ""),
            date_label=str(item.get("date_label") or ""),
            english=english,
        )
    return index


def save_ready_index_file(path: Path, index: Mapping[str, ReadyQuoteMatch]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": READY_INDEX_VERSION,
        "updated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "quotes": _ready_index_quotes_payload(index),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _ready_matches_from_rows(
    rows: list[list[str]],
    *,
    config: QuotesSourcesConfig,
    spreadsheet_name: str,
    tab_title: str,
    english_names: list[str],
    date_names: list[str],
) -> dict[str, ReadyQuoteMatch]:
    matches: dict[str, ReadyQuoteMatch] = {}
    if not rows:
        return matches
    headers = rows[0]
    english_index = _column_index(headers, *english_names)
    if english_index is None:
        return matches
    date_index = _column_index(headers, *date_names)
    for row in rows[1:]:
        english = _cell(row, english_index)
        key = _normalize_english(english)
        if not key or key in matches:
            continue
        ready = extract_ready_text_from_row(row, headers, config)
        if not ready:
            continue
        matches[key] = ReadyQuoteMatch(
            ready=ready,
            spreadsheet_name=spreadsheet_name,
            tab_title=tab_title,
            date_label=_cell(row, date_index),
            english=english,
        )
    return matches


def index_workbook_ready_quotes(
    sheets: GoogleSheetsClient,
    config: QuotesSourcesConfig,
    *,
    spreadsheet_id: str,
    spreadsheet_name: str,
) -> dict[str, ReadyQuoteMatch]:
    """Read one year workbook with a single values:batchGet call."""
    dest_cfg = config.translated_quotes_drive
    sheet_cfg = config.quotes_sheet
    english_names = _english_column_candidates(config)
    date_names = _unique_names(
        str(dest_cfg.get("date_column") or ""),
        str(sheet_cfg.get("date_column") or ""),
        "Date",
    )
    tabs = sheets.list_tabs(spreadsheet_id)
    if not tabs:
        return {}
    ranges = [_sheet_a1_range(tab.title) for tab in tabs]
    rows_by_tab = sheets.batch_get_values(spreadsheet_id, ranges)
    index: dict[str, ReadyQuoteMatch] = {}
    for tab, rows in zip(tabs, rows_by_tab):
        for key, match in _ready_matches_from_rows(
            rows,
            config=config,
            spreadsheet_name=spreadsheet_name,
            tab_title=tab.title,
            english_names=english_names,
            date_names=date_names,
        ).items():
            if key not in index:
                index[key] = match
    return index


def _merge_ready_indexes(
    *,
    workbooks: list[tuple[str, str, int]],
    cached: Mapping[str, ReadyQuoteMatch],
    live_by_spreadsheet: Mapping[str, dict[str, ReadyQuoteMatch] | None],
) -> dict[str, ReadyQuoteMatch]:
    cached_by_book: dict[str, dict[str, ReadyQuoteMatch]] = {}
    for key, match in cached.items():
        cached_by_book.setdefault(match.spreadsheet_name, {})[key] = match

    merged: dict[str, ReadyQuoteMatch] = {}
    seen_names: set[str] = set()
    for _spreadsheet_id, spreadsheet_name, _year in workbooks:
        seen_names.add(spreadsheet_name)
        source = live_by_spreadsheet.get(spreadsheet_name)
        if source is None:
            source = cached_by_book.get(spreadsheet_name, {})
        for key, match in source.items():
            if key not in merged:
                merged[key] = match
    for spreadsheet_name, entries in cached_by_book.items():
        if spreadsheet_name in seen_names:
            continue
        for key, match in entries.items():
            if key not in merged:
                merged[key] = match
    return merged


def load_ready_translations_by_english(
    *,
    drive: GoogleDriveClient,
    sheets: GoogleSheetsClient,
    config: QuotesSourcesConfig,
    project_root: Path | None = None,
    cache_path: Path | None = None,
    persist: bool = True,
) -> tuple[dict[str, ReadyQuoteMatch], list[str]]:
    """
    Index approved Bulgarian text by normalized English quote.

    Uses ``data/quotes_ready_index.json`` as a persistent cache, then refreshes
    from Drive with one batch read per year workbook. Failed workbooks keep
    their cached rows. Newer years win when the same English appears twice.
    """
    warnings: list[str] = []
    path = cache_path or quotes_ready_index_path(project_root)
    try:
        cached = load_ready_index_file(path)
    except (OSError, json.JSONDecodeError) as exc:
        cached = {}
        warnings.append(f"Could not read Ready quote cache {path}: {exc}")

    try:
        workbooks = list_bulgarian_year_workbooks(drive, config)
    except (QuotesTextSyncError, QuotesConfigError, GoogleDriveError) as exc:
        if cached:
            warnings.append(
                f"Using cached Ready quotes ({len(cached)}); "
                f"could not list Bulgarian quote workbooks: {exc}"
            )
            return cached, warnings
        return {}, [f"Could not list Bulgarian quote workbooks: {exc}"]

    live_by_spreadsheet: dict[str, dict[str, ReadyQuoteMatch] | None] = {}
    for spreadsheet_id, spreadsheet_name, _year in workbooks:
        try:
            live_by_spreadsheet[spreadsheet_name] = index_workbook_ready_quotes(
                sheets,
                config,
                spreadsheet_id=spreadsheet_id,
                spreadsheet_name=spreadsheet_name,
            )
        except GoogleSheetsError as exc:
            live_by_spreadsheet[spreadsheet_name] = None
            detail = str(exc).split("\n", 1)[0]
            warnings.append(f"{spreadsheet_name}: {detail}")

    index = _merge_ready_indexes(
        workbooks=workbooks,
        cached=cached,
        live_by_spreadsheet=live_by_spreadsheet,
    )
    if persist:
        new_quotes = _ready_index_quotes_payload(index)
        old_quotes = _ready_index_quotes_payload(cached)
        if new_quotes != old_quotes:
            try:
                save_ready_index_file(path, index)
            except OSError as exc:
                warnings.append(f"Could not write Ready quote cache {path}: {exc}")
    return index, warnings


def ready_quote_retrieval_docs(
    ready_by_english: Mapping[str, ReadyQuoteMatch],
) -> list[Any]:
    """Turn the Ready cache into BM25 docs (English query text, Bulgarian Ready)."""
    from catalog_parser.translation.index import CorpusDoc

    docs: list[Any] = []
    seen: set[str] = set()
    for key, match in ready_by_english.items():
        english = (match.english or "").strip() or key
        ready = (match.ready or "").strip()
        if not english or not ready:
            continue
        norm = _normalize_english(english)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        docs.append(
            CorpusDoc(
                en=english,
                bg=ready,
                video_title=match.spreadsheet_name,
            )
        )
    return docs


def build_quote_retrieval_index(ready_by_english: Mapping[str, ReadyQuoteMatch]):
    from catalog_parser.translation.index import Bm25Index

    return Bm25Index(ready_quote_retrieval_docs(ready_by_english))


def build_quote_translation_messages(
    english: str,
    examples: list[Any],
) -> list[dict[str, str]]:
    from catalog_parser.translation.rag_translate import format_examples
    from media_publisher.languages import selected_language

    language = selected_language()
    ingest = language.require_ingest()
    name = language.name
    system = (
        f"You are a professional translator for Sadhguru daily quotes into {name}. "
        "Preserve meaning and spiritual tone. Keep the quote concise and natural. "
        f"Use {name} quotation marks {ingest.quote_open}…{ingest.quote_close} "
        "when the English uses quotation marks. "
        "Match the terminology and register of the example Ready translations. "
        "Do not add attribution, explanations, or hashtags. "
        f"Return only the {name} quote text."
    )
    user = (
        f"Translate this daily quote into {name}.\n"
        "Use the examples only as style references unless one is the same quote.\n\n"
        f"Examples from prior approved quote translations:\n"
        f"{format_examples(examples)}\n\n"
        f"English quote:\n{english}\n\n"
        f"Respond with {name} quote text only."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _ready_archive_for_translation(
    ready_by_english: Mapping[str, ReadyQuoteMatch] | None,
    project_root: Path | None,
) -> dict[str, ReadyQuoteMatch]:
    if ready_by_english is not None:
        return dict(ready_by_english)
    try:
        return load_ready_index_file(quotes_ready_index_path(project_root))
    except (OSError, json.JSONDecodeError):
        return {}


def translate_quote_text(
    english: str,
    *,
    project_root: Path | None = None,
    ready_by_english: Mapping[str, ReadyQuoteMatch] | None = None,
    retrieval_index: Any | None = None,
    top_k: int = DEFAULT_QUOTE_RAG_TOP_K,
) -> str:
    """Translate a daily quote using Ready-cache RAG examples when available."""
    from catalog_parser.translation.prefill import ai_prefill_enabled
    from catalog_parser.translation.rag_translate import (
        chat_completion,
        chat_config_from_env,
        translation_provider_disabled,
    )

    text = (english or "").strip()
    if not text:
        raise QuotesTextSyncError("Cannot translate empty quote text")

    archive = _ready_archive_for_translation(ready_by_english, project_root)
    exact = lookup_ready_by_english(text, archive)
    if exact is not None:
        return exact.ready

    if translation_provider_disabled() or not ai_prefill_enabled():
        raise QuotesTextSyncError(
            "AI translation is disabled "
            "(set TRANSLATION_PROVIDER=anthropic|openai and TRANSLATION_API_KEY)"
        )

    index = retrieval_index
    if index is None and archive:
        index = build_quote_retrieval_index(archive)

    examples: list[Any] = []
    if index is not None:
        hits = list(index.retrieve(text, k=max(top_k, 1)))
        query_key = _normalize_english(text)
        for hit in hits:
            if _normalize_english(hit.en) == query_key:
                return hit.bg
        examples = hits[:top_k]

    chat = chat_config_from_env()
    translated = chat_completion(
        build_quote_translation_messages(text, examples),
        chat,
    ).strip()
    if not translated:
        raise QuotesTextSyncError("Empty quote translation returned by the model")
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
    comment_index = _column_index(
        headers,
        str(dest_cfg.get("comment_column") or sheet_cfg.get("comment_column", "Comment")),
        "Comment",
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
        "comment": comment_index if comment_index is not None else -1,
    }


def reuse_source_comment(match: ReadyQuoteMatch) -> str:
    if match.date_label.strip():
        return f"Reused from {match.date_label.strip()}"
    if match.tab_title.strip():
        return f"Reused from {match.tab_title.strip()}"
    return "Reused from archive"


def _ensure_destination_columns(
    *,
    sheets: GoogleSheetsClient,
    dest: DestinationSheetRef,
    headers: list[str],
    columns: dict[str, int],
    extras: tuple[tuple[str, str], ...],
) -> list[str]:
    updated = list(headers)
    added = False
    for key, title in extras:
        if columns.get(key, -1) < 0:
            updated.append(title)
            columns[key] = len(updated) - 1
            added = True
    if added:
        escaped = dest.tab.title.replace("'", "''")
        sheets.batch_update_values(
            dest.spreadsheet_id,
            [(f"'{escaped}'!A1", [updated])],
        )
    return updated


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
    ready_by_english: Mapping[str, ReadyQuoteMatch] | None = None,
) -> QuotesTextSyncResult:
    log = print_line or (lambda _msg: None)
    result = QuotesTextSyncResult()
    archive = ready_by_english
    if archive is None:
        archive, index_warnings = load_ready_translations_by_english(
            drive=drive,
            sheets=sheets,
            config=config,
        )
        result.warnings.extend(index_warnings)

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
    headers = _ensure_destination_columns(
        sheets=sheets,
        dest=dest,
        headers=headers,
        columns=columns,
        extras=(
            ("edited", "Edited"),
            ("ready", "Ready"),
            ("comment", "Comment"),
        ),
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
    format_clears: list[tuple[int, int, int]] = []
    stale_ready_cells: list[tuple[int, int, int]] = []
    reset_ready_backgrounds: list[tuple[int, int, int]] = []
    next_append_row = len(rows) + 1
    translator = translate_fn
    if translator is None:
        retrieval_index = (
            build_quote_retrieval_index(archive) if archive else None
        )
        translator = lambda text: translate_quote_text(
            text,
            project_root=project_root,
            ready_by_english=archive,
            retrieval_index=retrieval_index,
        )

    for quote in english_rows:
        existing = existing_by_day.get(quote.day)
        existing_english = _cell(existing[1], columns["english"]) if existing else ""
        english_unchanged = bool(
            existing
            and _normalize_text(existing_english) == _normalize_text(quote.english)
        )
        existing_ready = ""
        if existing:
            existing_ready = (
                extract_ready_text_from_row(existing[1], headers, config) or ""
            )
        if english_unchanged and existing_ready:
            continue
        if english_unchanged and lookup_ready_by_english(quote.english, archive) is None:
            continue

        action = "updated" if existing else "added"
        if existing is None:
            row_number = next_append_row
            next_append_row += 1
            prior_row: list[str] = []
        else:
            row_number = existing[0]
            prior_row = existing[1]

        width = max(
            len(headers),
            columns["edited"] + 1,
            columns["translation"] + 1,
            columns["ready"] + 1,
            columns["comment"] + 1,
        )
        new_row = [""] * width
        for index, value in enumerate(prior_row):
            if index < width:
                new_row[index] = value
        new_row[columns["date"]] = quote.date_label
        new_row[columns["english"]] = quote.english

        translation_detail = ""
        reuse_text: str | None = None
        match = lookup_ready_by_english(quote.english, archive)
        if match is not None:
            reuse_text = match.ready
            origin = f"{match.spreadsheet_name} / {match.tab_title}"
            if match.date_label:
                origin = f"{origin} ({match.date_label})"
            translation_detail = f"reused Ready from {origin}"
            new_row[columns["ready"]] = reuse_text
            if columns["comment"] >= 0:
                new_row[columns["comment"]] = reuse_source_comment(match)
            if columns["ready"] >= 0:
                reset_ready_backgrounds.append(
                    (dest.tab.sheet_id, row_number, columns["ready"])
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

        stale_ready = bool(
            existing
            and not english_unchanged
            and existing_ready
            and not reuse_text
            and columns["ready"] >= 0
        )
        if stale_ready:
            stale_ready_cells.append(
                (dest.tab.sheet_id, row_number, columns["ready"])
            )
            stale_note = "stale Ready highlighted"
            translation_detail = (
                f"{translation_detail}; {stale_note}"
                if translation_detail
                else stale_note
            )

        for column_key, column_index in (
            ("date", columns["date"]),
            ("english", columns["english"]),
            ("translation", columns["translation"]),
            ("ready", columns["ready"]),
            ("comment", columns["comment"]),
        ):
            if column_index < 0:
                continue
            if column_key in {"ready", "comment"} and not reuse_text:
                continue
            if column_key == "translation" and (reuse_text or not new_row[column_index]):
                continue
            updates.append(
                (
                    a1_cell(dest.tab.title, row_number, column_index),
                    [[new_row[column_index]]],
                )
            )
            if column_key in {"ready", "translation"}:
                format_clears.append(
                    (dest.tab.sheet_id, row_number, column_index)
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
        sheets.batch_update_values(
            dest.spreadsheet_id,
            updates,
            value_input_option="RAW",
        )
        sheets.clear_cells_text_format(dest.spreadsheet_id, format_clears)
    if stale_ready_cells:
        sheets.set_cells_background(
            dest.spreadsheet_id,
            stale_ready_cells,
            STALE_READY_BACKGROUND,
        )
    if reset_ready_backgrounds:
        sheets.set_cells_background(
            dest.spreadsheet_id,
            reset_ready_backgrounds,
            None,
        )

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
    log = print_line or (lambda _msg: None)
    ready_by_english, index_warnings = load_ready_translations_by_english(
        drive=drive,
        sheets=sheets,
        config=config,
        project_root=project_root,
    )
    combined.warnings.extend(index_warnings)
    cache_name = quotes_ready_index_path(project_root).name
    log(
        f"Indexed {len(ready_by_english)} Ready quotes from "
        f"{len({match.spreadsheet_name for match in ready_by_english.values()})} "
        f"Bulgarian workbooks ({cache_name})"
    )
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
            ready_by_english=ready_by_english,
        )
        combined.changes.extend(month_result.changes)
        combined.warnings.extend(month_result.warnings)
    return combined
