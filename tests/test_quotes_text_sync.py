from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from media_publisher.quotes_text_sync import (
    DestinationSheetRef,
    EnglishQuoteRow,
    ReadyQuoteMatch,
    _merge_ready_indexes,
    _normalize_english,
    _year_from_bulgarian_workbook_name,
    build_quote_retrieval_index,
    extract_ready_text_from_row,
    find_ready_column_index,
    find_tab_by_candidates,
    list_bulgarian_year_workbooks,
    load_ready_index_file,
    load_ready_translations_by_english,
    month_file_name_candidates,
    reuse_source_comment,
    save_ready_index_file,
    sync_month_quote_texts,
    translate_quote_text,
    year_workbook_name,
)
from media_publisher.sources.google_drive import (
    GOOGLE_SHEETS_MIME_TYPE,
    DriveFile,
)
from media_publisher.sources.google_sheets import SheetTab
from media_publisher.sources.quotes_config import (
    extract_spreadsheet_id,
    load_quotes_sources_config,
)


class QuotesTextConfigTests(unittest.TestCase):
    def test_month_file_name_candidates(self) -> None:
        names = month_file_name_candidates(2026, 9)
        self.assertIn("Sep 2026", names)
        self.assertIn("September 2026", names)
        self.assertIn("09 Sep 2026", names)

    def test_year_workbook_name(self) -> None:
        self.assertEqual(
            year_workbook_name(2024),
            "Sadhguru Quotes Bulgarian 2024",
        )
        self.assertEqual(
            year_workbook_name(2099),
            "Sadhguru Quotes Bulgarian 2099",
        )

    def test_year_from_bulgarian_workbook_name(self) -> None:
        self.assertEqual(
            _year_from_bulgarian_workbook_name("Sadhguru Quotes Bulgarian 2022"),
            2022,
        )
        self.assertEqual(
            _year_from_bulgarian_workbook_name(
                "Sadhguru Quotes Bulgarian 2023.xlsx"
            ),
            2023,
        )
        self.assertIsNone(
            _year_from_bulgarian_workbook_name("Sadhguru Quote 2026")
        )

    def test_normalize_english_ignores_case_whitespace_and_quotes(self) -> None:
        self.assertEqual(
            _normalize_english("  The “teacher” is — needed "),
            _normalize_english("the \"teacher\" is - needed"),
        )

    def test_matches_year_workbook_name_ignores_xlsx(self) -> None:
        from media_publisher.quotes_text_sync import matches_year_workbook_name

        year = 2031
        expected = f"Sadhguru Quotes Bulgarian {year}"
        self.assertTrue(matches_year_workbook_name(expected, expected))
        self.assertTrue(
            matches_year_workbook_name(f"{expected}.xlsx", expected)
        )
        self.assertFalse(
            matches_year_workbook_name(f"Sadhguru Quote {year}.xlsx", expected)
        )

    def test_find_tab_prefers_full_month_name(self) -> None:
        tabs = [
            SheetTab(sheet_id=1, title="Jul 2022"),
            SheetTab(sheet_id=2, title="July 2022"),
        ]
        # Candidates list puts abbr first; either match is fine as long as one hits.
        match = find_tab_by_candidates(tabs, ["Jul 2022", "July 2022"])
        self.assertIsNotNone(match)
        self.assertEqual(match.title, "Jul 2022")
        only_full = find_tab_by_candidates(
            [SheetTab(sheet_id=2, title="July 2022")],
            ["Jul 2022", "July 2022"],
        )
        self.assertEqual(only_full.title, "July 2022")

    def test_load_config_applies_english_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "quotes_sources.json"
            path.write_text(
                json.dumps(
                    {
                        "quotes_sheet": {"text_bg_column": "Translation"},
                        "backgrounds_drive": {"variants": {}},
                        "canva_templates": {},
                        "renders": {},
                    }
                ),
                encoding="utf-8",
            )
            env = {
                "ENGLISH_QUOTES_URL": (
                    "https://docs.google.com/spreadsheets/d/"
                    "1cZbIZB8uMdDNDTJVrtq8Tjo5C1eVmGZvGjZfXAl_U60/edit"
                ),
            }
            with patch.dict(os.environ, env, clear=False):
                config = load_quotes_sources_config(path)
            self.assertEqual(
                config.english_spreadsheet_id,
                "1cZbIZB8uMdDNDTJVrtq8Tjo5C1eVmGZvGjZfXAl_U60",
            )


class QuotesTextSyncLogicTests(unittest.TestCase):
    def _config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "quotes_sources.json"
            path.write_text(
                json.dumps(
                    {
                        "english_quotes": {
                            "spreadsheet_id": "english-sheet",
                            "date_column": "Date",
                            "english_columns": ["English"],
                            "previously_posted_column": "Previously Posted on",
                        },
                        "translated_quotes_drive": {
                            "folder_id": "folder-1",
                            "date_column": "Date",
                            "english_column": "English",
                            "translation_column": "Translation",
                            "edited_column": "Edited",
                            "ready_column": "Ready",
                            "comment_column": "Comment",
                        },
                        "quotes_sheet": {
                            "text_bg_column": "Translation",
                            "text_en_column": "English",
                            "ready_column": "Ready",
                            "date_column": "Date",
                        },
                        "backgrounds_drive": {"variants": {}},
                        "canva_templates": {},
                        "renders": {},
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "ENGLISH_QUOTES_URL": "",
                },
                clear=False,
            ):
                return load_quotes_sources_config(path)

    def test_sync_reuses_ready_by_english_match(self) -> None:
        config = self._config()
        sheets = MagicMock()
        drive = MagicMock()

        english_rows = [
            EnglishQuoteRow(
                day=1,
                publish_date=date(2026, 9, 1),
                date_label="1 Sep 2026",
                english="New quote one",
                previously_posted_on=None,
                previously_posted_label="",
            ),
            EnglishQuoteRow(
                day=2,
                publish_date=date(2026, 9, 2),
                date_label="2 Sep 2026",
                english="Brand new quote",
                previously_posted_on=None,
                previously_posted_label="",
            ),
            EnglishQuoteRow(
                day=5,
                publish_date=date(2026, 9, 5),
                date_label="5 Sep 2026",
                english="The significance of a teacher",
                previously_posted_on=None,
                previously_posted_label="16-Aug-22\n\n5-Aug-22",
            ),
        ]
        archive = {
            _normalize_english("New quote one"): ReadyQuoteMatch(
                ready="Стара готова цитат",
                spreadsheet_name="Sadhguru Quotes Bulgarian 2025",
                tab_title="Mar 2025",
                date_label="10 Mar 2025",
            ),
            _normalize_english("The significance of a teacher"): ReadyQuoteMatch(
                ready="Значението на учителя",
                spreadsheet_name="Sadhguru Quotes Bulgarian 2022",
                tab_title="Aug 2022",
                date_label="16 Aug 2022",
            ),
        }

        dest = DestinationSheetRef(
            spreadsheet_id="dest-sep",
            spreadsheet_name="Sep 2026",
            tab=SheetTab(sheet_id=1, title="Sep 2026"),
        )

        with (
            patch(
                "media_publisher.quotes_text_sync.load_english_quote_rows",
                return_value=english_rows,
            ),
            patch(
                "media_publisher.quotes_text_sync.resolve_destination_month_sheet",
                return_value=dest,
            ),
            patch(
                "media_publisher.quotes_text_sync._read_sheet_rows",
                return_value=[
                    ["Date", "English", "Translation", "Edited", "Ready", "Comment"],
                ],
            ),
        ):
            result = sync_month_quote_texts(
                config=config,
                sheets=sheets,
                drive=drive,
                year=2026,
                month=9,
                translate_fn=lambda text: f"BG:{text}",
                ready_by_english=archive,
            )

        self.assertEqual(result.added_count, 3)
        self.assertEqual(result.reused_count, 2)
        self.assertEqual(result.translated_count, 1)
        self.assertEqual(result.warnings, [])
        sheets.batch_update_values.assert_called()
        self.assertEqual(
            sheets.batch_update_values.call_args.kwargs.get("value_input_option"),
            "RAW",
        )
        sheets.clear_cells_text_format.assert_called()
        written = sheets.batch_update_values.call_args[0][1]
        values = {range_a1: cells[0][0] for range_a1, cells in written}
        self.assertEqual(values["'Sep 2026'!E2"], "Стара готова цитат")
        self.assertEqual(values["'Sep 2026'!F2"], "Reused from 10 Mar 2025")
        self.assertEqual(values["'Sep 2026'!E4"], "Значението на учителя")
        self.assertEqual(values["'Sep 2026'!F4"], "Reused from 16 Aug 2022")
        self.assertIn("BG:Brand new quote", values.values())
        self.assertNotIn("'Sep 2026'!D2", values)
        self.assertNotIn("'Sep 2026'!D4", values)

    def test_sync_skips_unchanged_english(self) -> None:
        config = self._config()
        sheets = MagicMock()
        drive = MagicMock()
        english_rows = [
            EnglishQuoteRow(
                day=5,
                publish_date=date(2026, 9, 5),
                date_label="5 Sep 2026",
                english="Same text",
                previously_posted_on=None,
                previously_posted_label="",
            )
        ]
        dest = DestinationSheetRef(
            spreadsheet_id="dest-sep",
            spreadsheet_name="Sep 2026",
            tab=SheetTab(sheet_id=1, title="Sep 2026"),
        )
        with (
            patch(
                "media_publisher.quotes_text_sync.load_english_quote_rows",
                return_value=english_rows,
            ),
            patch(
                "media_publisher.quotes_text_sync.resolve_destination_month_sheet",
                return_value=dest,
            ),
            patch(
                "media_publisher.quotes_text_sync._read_sheet_rows",
                return_value=[
                    ["Date", "English", "Translation", "Edited", "Ready", "Comment"],
                    ["5 Sep 2026", "Same text", "Стар", "", "", ""],
                ],
            ),
        ):
            result = sync_month_quote_texts(
                config=config,
                sheets=sheets,
                drive=drive,
                year=2026,
                month=9,
                translate_fn=lambda text: "SHOULD_NOT_RUN",
                ready_by_english={},
            )
        self.assertEqual(result.changes, [])
        sheets.batch_update_values.assert_not_called()

    def test_sync_fills_ready_when_english_already_matches_archive(self) -> None:
        config = self._config()
        sheets = MagicMock()
        drive = MagicMock()
        english_rows = [
            EnglishQuoteRow(
                day=5,
                publish_date=date(2026, 9, 5),
                date_label="5 Sep 2026",
                english="The significance of a teacher",
                previously_posted_on=None,
                previously_posted_label="",
            )
        ]
        dest = DestinationSheetRef(
            spreadsheet_id="dest-sep",
            spreadsheet_name="Sep 2026",
            tab=SheetTab(sheet_id=1, title="Sep 2026"),
        )
        archive = {
            _normalize_english("The significance of a teacher"): ReadyQuoteMatch(
                ready="Значението на учителя",
                spreadsheet_name="Sadhguru Quotes Bulgarian 2022",
                tab_title="Aug 2022",
                date_label="16 Aug 2022",
            ),
        }
        with (
            patch(
                "media_publisher.quotes_text_sync.load_english_quote_rows",
                return_value=english_rows,
            ),
            patch(
                "media_publisher.quotes_text_sync.resolve_destination_month_sheet",
                return_value=dest,
            ),
            patch(
                "media_publisher.quotes_text_sync._read_sheet_rows",
                return_value=[
                    ["Date", "English", "Translation", "Edited", "Ready", "Comment"],
                    ["5 Sep 2026", "The significance of a teacher", "", "", "", ""],
                ],
            ),
        ):
            result = sync_month_quote_texts(
                config=config,
                sheets=sheets,
                drive=drive,
                year=2026,
                month=9,
                translate_fn=lambda text: "SHOULD_NOT_RUN",
                ready_by_english=archive,
            )
        self.assertEqual(result.reused_count, 1)
        written = sheets.batch_update_values.call_args[0][1]
        values = {range_a1: cells[0][0] for range_a1, cells in written}
        self.assertEqual(values["'Sep 2026'!E2"], "Значението на учителя")
        self.assertEqual(values["'Sep 2026'!F2"], "Reused from 16 Aug 2022")
        self.assertNotIn("'Sep 2026'!D2", values)

    def test_sync_fills_ready_even_when_edited_already_has_reuse(self) -> None:
        config = self._config()
        sheets = MagicMock()
        drive = MagicMock()
        english_rows = [
            EnglishQuoteRow(
                day=5,
                publish_date=date(2026, 9, 5),
                date_label="5 Sep 2026",
                english="The significance of a teacher",
                previously_posted_on=None,
                previously_posted_label="",
            )
        ]
        dest = DestinationSheetRef(
            spreadsheet_id="dest-sep",
            spreadsheet_name="Sep 2026",
            tab=SheetTab(sheet_id=1, title="Sep 2026"),
        )
        archive = {
            _normalize_english("The significance of a teacher"): ReadyQuoteMatch(
                ready="Значението на учителя",
                spreadsheet_name="Sadhguru Quotes Bulgarian 2022",
                tab_title="Aug 2022",
                date_label="16 Aug 2022",
            ),
        }
        with (
            patch(
                "media_publisher.quotes_text_sync.load_english_quote_rows",
                return_value=english_rows,
            ),
            patch(
                "media_publisher.quotes_text_sync.resolve_destination_month_sheet",
                return_value=dest,
            ),
            patch(
                "media_publisher.quotes_text_sync._read_sheet_rows",
                return_value=[
                    ["Date", "English", "Translation", "Edited", "Ready", "Comment"],
                    [
                        "5 Sep 2026",
                        "The significance of a teacher",
                        "",
                        "Значението на учителя",
                        "",
                        "",
                    ],
                ],
            ),
        ):
            result = sync_month_quote_texts(
                config=config,
                sheets=sheets,
                drive=drive,
                year=2026,
                month=9,
                translate_fn=lambda text: "SHOULD_NOT_RUN",
                ready_by_english=archive,
            )
        self.assertEqual(result.reused_count, 1)
        written = sheets.batch_update_values.call_args[0][1]
        values = {range_a1: cells[0][0] for range_a1, cells in written}
        self.assertEqual(values["'Sep 2026'!E2"], "Значението на учителя")
        self.assertEqual(values["'Sep 2026'!F2"], "Reused from 16 Aug 2022")
        self.assertNotIn("'Sep 2026'!D2", values)

    def test_sync_highlights_stale_ready_when_english_changes(self) -> None:
        config = self._config()
        sheets = MagicMock()
        drive = MagicMock()
        english_rows = [
            EnglishQuoteRow(
                day=5,
                publish_date=date(2026, 9, 5),
                date_label="5 Sep 2026",
                english="Updated english quote",
                previously_posted_on=None,
                previously_posted_label="",
            )
        ]
        dest = DestinationSheetRef(
            spreadsheet_id="dest-sep",
            spreadsheet_name="Sep 2026",
            tab=SheetTab(sheet_id=1, title="Sep 2026"),
        )
        with (
            patch(
                "media_publisher.quotes_text_sync.load_english_quote_rows",
                return_value=english_rows,
            ),
            patch(
                "media_publisher.quotes_text_sync.resolve_destination_month_sheet",
                return_value=dest,
            ),
            patch(
                "media_publisher.quotes_text_sync._read_sheet_rows",
                return_value=[
                    ["Date", "English", "Translation", "Edited", "Ready", "Comment"],
                    [
                        "5 Sep 2026",
                        "Old english quote",
                        "",
                        "",
                        "Стара готовност",
                        "",
                    ],
                ],
            ),
        ):
            result = sync_month_quote_texts(
                config=config,
                sheets=sheets,
                drive=drive,
                year=2026,
                month=9,
                translate_fn=lambda text: f"BG:{text}",
                ready_by_english={},
            )
        details = [change.detail for change in result.changes]
        self.assertTrue(any("stale Ready highlighted" in detail for detail in details))
        written = sheets.batch_update_values.call_args[0][1]
        values = {range_a1: cells[0][0] for range_a1, cells in written}
        self.assertEqual(values["'Sep 2026'!B2"], "Updated english quote")
        self.assertEqual(values["'Sep 2026'!C2"], "BG:Updated english quote")
        self.assertNotIn("'Sep 2026'!E2", values)
        sheets.set_cells_background.assert_called_once_with(
            "dest-sep",
            [(1, 2, 4)],
            {"red": 1.0, "green": 1.0, "blue": 0.0},
        )

    def test_sync_clears_ready_highlight_when_reuse_replaces_stale_text(self) -> None:
        config = self._config()
        sheets = MagicMock()
        drive = MagicMock()
        english_rows = [
            EnglishQuoteRow(
                day=5,
                publish_date=date(2026, 9, 5),
                date_label="5 Sep 2026",
                english="The significance of a teacher",
                previously_posted_on=None,
                previously_posted_label="",
            )
        ]
        dest = DestinationSheetRef(
            spreadsheet_id="dest-sep",
            spreadsheet_name="Sep 2026",
            tab=SheetTab(sheet_id=1, title="Sep 2026"),
        )
        archive = {
            _normalize_english("The significance of a teacher"): ReadyQuoteMatch(
                ready="Значението на учителя",
                spreadsheet_name="Sadhguru Quotes Bulgarian 2022",
                tab_title="Aug 2022",
                date_label="16 Aug 2022",
            ),
        }
        with (
            patch(
                "media_publisher.quotes_text_sync.load_english_quote_rows",
                return_value=english_rows,
            ),
            patch(
                "media_publisher.quotes_text_sync.resolve_destination_month_sheet",
                return_value=dest,
            ),
            patch(
                "media_publisher.quotes_text_sync._read_sheet_rows",
                return_value=[
                    ["Date", "English", "Translation", "Edited", "Ready", "Comment"],
                    [
                        "5 Sep 2026",
                        "Old english quote",
                        "",
                        "",
                        "Стара готовност",
                        "",
                    ],
                ],
            ),
        ):
            result = sync_month_quote_texts(
                config=config,
                sheets=sheets,
                drive=drive,
                year=2026,
                month=9,
                translate_fn=lambda text: "SHOULD_NOT_RUN",
                ready_by_english=archive,
            )
        self.assertEqual(result.reused_count, 1)
        written = sheets.batch_update_values.call_args[0][1]
        values = {range_a1: cells[0][0] for range_a1, cells in written}
        self.assertEqual(values["'Sep 2026'!E2"], "Значението на учителя")
        sheets.set_cells_background.assert_called_once_with(
            "dest-sep",
            [(1, 2, 4)],
            None,
        )

    def test_reuse_source_comment(self) -> None:
        self.assertEqual(
            reuse_source_comment(
                ReadyQuoteMatch(
                    ready="Текст",
                    spreadsheet_name="Sadhguru Quotes Bulgarian 2022",
                    tab_title="Aug 2022",
                    date_label="16 Aug 2022",
                )
            ),
            "Reused from 16 Aug 2022",
        )
        self.assertEqual(
            reuse_source_comment(
                ReadyQuoteMatch(
                    ready="Текст",
                    spreadsheet_name="Sadhguru Quotes Bulgarian 2022",
                    tab_title="Aug 2022",
                    date_label="",
                )
            ),
            "Reused from Aug 2022",
        )

    def test_list_bulgarian_year_workbooks_skips_excel_and_other_names(self) -> None:
        config = self._config()
        drive = MagicMock()
        drive.list_spreadsheets.return_value = [
            DriveFile(
                id="xlsx-2022",
                name="Sadhguru Quotes Bulgarian 2022.xlsx",
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            DriveFile(
                id="sheet-2024",
                name="Sadhguru Quotes Bulgarian 2024",
                mime_type=GOOGLE_SHEETS_MIME_TYPE,
            ),
            DriveFile(
                id="sheet-2021",
                name="Sadhguru Quotes Bulgarian 2021",
                mime_type=GOOGLE_SHEETS_MIME_TYPE,
            ),
            DriveFile(
                id="english",
                name="Sadhguru Quote 2026",
                mime_type=GOOGLE_SHEETS_MIME_TYPE,
            ),
        ]
        with patch(
            "media_publisher.quotes_text_sync.resolve_quotes_folder_id",
            return_value="quotes-folder",
        ):
            workbooks = list_bulgarian_year_workbooks(drive, config)
        self.assertEqual(
            workbooks,
            [
                ("sheet-2024", "Sadhguru Quotes Bulgarian 2024", 2024),
                ("sheet-2021", "Sadhguru Quotes Bulgarian 2021", 2021),
            ],
        )

    def test_load_ready_index_matches_english_and_skips_empty_ready(self) -> None:
        config = self._config()
        drive = MagicMock()
        sheets = MagicMock()
        sheets.list_tabs.return_value = [SheetTab(sheet_id=1, title="Aug 2022")]
        sheets.batch_get_values.return_value = [
            [
                ["Date", "English", "Translation", "Edited", "Ready"],
                ["16 Aug 2022", "The significance of a teacher", "", "", "Значението"],
                ["5 Aug 2022", "Another quote", "Чернова", "Редакция", ""],
            ]
        ]

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch(
                "media_publisher.quotes_text_sync.list_bulgarian_year_workbooks",
                return_value=[
                    ("bg-2022", "Sadhguru Quotes Bulgarian 2022", 2022),
                ],
            ),
        ):
            index, warnings = load_ready_translations_by_english(
                drive=drive,
                sheets=sheets,
                config=config,
                cache_path=Path(tmpdir) / "quotes_ready_index.json",
                persist=False,
            )
        self.assertEqual(warnings, [])
        self.assertEqual(
            index[_normalize_english("the significance of a teacher")].ready,
            "Значението",
        )
        self.assertNotIn(_normalize_english("Another quote"), index)

    def test_ready_index_file_round_trip(self) -> None:
        match = ReadyQuoteMatch(
            ready="Готов текст",
            spreadsheet_name="Sadhguru Quotes Bulgarian 2022",
            tab_title="Aug 2022",
            date_label="16 Aug 2022",
            english="The significance of a teacher",
        )
        index = {_normalize_english(match.english): match}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "quotes_ready_index.json"
            save_ready_index_file(path, index)
            loaded = load_ready_index_file(path)
        self.assertEqual(loaded[_normalize_english(match.english)].ready, "Готов текст")
        self.assertEqual(
            loaded[_normalize_english(match.english)].spreadsheet_name,
            "Sadhguru Quotes Bulgarian 2022",
        )

    def test_merge_keeps_cache_when_live_workbook_fails(self) -> None:
        cached_key = _normalize_english("Old quote")
        live_key = _normalize_english("New quote")
        cached = {
            cached_key: ReadyQuoteMatch(
                ready="Стар",
                spreadsheet_name="Sadhguru Quotes Bulgarian 2022",
                tab_title="Aug 2022",
                english="Old quote",
            )
        }
        live = {
            "Sadhguru Quotes Bulgarian 2024": {
                live_key: ReadyQuoteMatch(
                    ready="Нов",
                    spreadsheet_name="Sadhguru Quotes Bulgarian 2024",
                    tab_title="Sep 2024",
                    english="New quote",
                )
            },
            "Sadhguru Quotes Bulgarian 2022": None,
        }
        merged = _merge_ready_indexes(
            workbooks=[
                ("id-2024", "Sadhguru Quotes Bulgarian 2024", 2024),
                ("id-2022", "Sadhguru Quotes Bulgarian 2022", 2022),
            ],
            cached=cached,
            live_by_spreadsheet=live,
        )
        self.assertEqual(merged[live_key].ready, "Нов")
        self.assertEqual(merged[cached_key].ready, "Стар")

    def _ready_archive(self) -> dict[str, ReadyQuoteMatch]:
        rows = [
            (
                "Joy is the source of all creation.",
                "Радостта е източникът на цялото творение.",
            ),
            (
                "Joy is not something that you do.",
                "Радостта не е нещо, което правите.",
            ),
            (
                "The body is just a heap of food.",
                "Тялото е просто купчина храна.",
            ),
        ]
        return {
            _normalize_english(english): ReadyQuoteMatch(
                ready=ready,
                spreadsheet_name="Sadhguru Quotes Bulgarian 2024",
                tab_title="Jan 2024",
                english=english,
            )
            for english, ready in rows
        }

    def test_quote_retrieval_prefers_similar_ready_quotes(self) -> None:
        archive = self._ready_archive()
        index = build_quote_retrieval_index(archive)
        hits = index.retrieve("Joy is not an achievement", k=2)
        self.assertGreaterEqual(len(hits), 1)
        self.assertTrue(all("Joy" in hit.en for hit in hits))
        self.assertNotIn("heap of food", hits[0].en)

    def test_translate_quote_text_uses_ready_cache_examples(self) -> None:
        archive = self._ready_archive()
        captured: dict[str, object] = {}

        def fake_complete(messages, _config, session=None):
            captured["user"] = messages[1]["content"]
            return "Нов превод"

        with (
            patch(
                "catalog_parser.translation.prefill.ai_prefill_enabled",
                return_value=True,
            ),
            patch(
                "catalog_parser.translation.rag_translate.translation_provider_disabled",
                return_value=False,
            ),
            patch(
                "catalog_parser.translation.rag_translate.chat_config_from_env",
                return_value=MagicMock(),
            ),
            patch(
                "catalog_parser.translation.rag_translate.chat_completion",
                side_effect=fake_complete,
            ),
            patch(
                "catalog_parser.translation.rag_translate.translate_metadata_field",
            ) as metadata,
        ):
            result = translate_quote_text(
                "Joy cannot be pursued",
                ready_by_english=archive,
            )
        self.assertEqual(result, "Нов превод")
        metadata.assert_not_called()
        user = str(captured["user"])
        self.assertIn("approved quote translations", user)
        self.assertIn("Радостта", user)
        self.assertNotIn("YouTube title", user)

    def test_translate_quote_text_returns_exact_ready_without_ai(self) -> None:
        archive = self._ready_archive()
        with patch(
            "catalog_parser.translation.rag_translate.chat_completion",
        ) as complete:
            result = translate_quote_text(
                "Joy is not something that you do.",
                ready_by_english=archive,
            )
        self.assertEqual(result, "Радостта не е нещо, което правите.")
        complete.assert_not_called()


class ReadyColumnLayoutTests(unittest.TestCase):
    def _config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "quotes_sources.json"
            path.write_text(
                json.dumps(
                    {
                        "translated_quotes_drive": {
                            "folder_id": "folder-1",
                            "ready_column": "Ready",
                            "ready_column_candidates": ["Ready", "Proofread"],
                            "edited_column": "Edited",
                            "translation_column": "Translation",
                        },
                        "quotes_sheet": {
                            "ready_column": "Ready ",
                            "edited_column": "Edited",
                            "text_bg_column": "Translation",
                        },
                        "backgrounds_drive": {"variants": {}},
                        "canva_templates": {},
                        "renders": {},
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "ENGLISH_QUOTES_URL": "",
                },
                clear=False,
            ):
                return load_quotes_sources_config(path)

    def test_ready_found_regardless_of_column_order(self) -> None:
        config = self._config()
        headers = ["Date", "English", "Стела", "Георги", "Ready ", "Comment"]
        self.assertEqual(find_ready_column_index(headers, config), 4)
        text = extract_ready_text_from_row(
            ["31 Jul 2022", "EN", "", "", "Готов текст", ""],
            headers,
            config,
        )
        self.assertEqual(text, "Готов текст")

    def test_proofread_used_when_ready_missing(self) -> None:
        config = self._config()
        headers = ["Date", "English", "Translation", "Edited", "Proofread", "Comment"]
        self.assertEqual(find_ready_column_index(headers, config), 4)
        text = extract_ready_text_from_row(
            ["1 Jul 2023", "EN", "AI", "", "Коригиран текст", ""],
            headers,
            config,
        )
        self.assertEqual(text, "Коригиран текст")

    def test_does_not_fall_back_to_edited_or_translation(self) -> None:
        config = self._config()
        headers = ["Date", "English", "Translation", "Edited", "Comment"]
        self.assertIsNone(find_ready_column_index(headers, config))
        self.assertIsNone(
            extract_ready_text_from_row(
                ["1 Jan 2024", "EN", "Чернова", "Редакция", ""],
                headers,
                config,
            )
        )
        headers_with_empty_ready = [
            "Date",
            "English",
            "Translation",
            "Edited",
            "Ready",
            "Comment",
        ]
        self.assertIsNone(
            extract_ready_text_from_row(
                ["1 Jan 2024", "EN", "Чернова", "Редакция", "", ""],
                headers_with_empty_ready,
                config,
            )
        )
    def test_extract_spreadsheet_id_still_works(self) -> None:
        self.assertEqual(
            extract_spreadsheet_id("abc123XYZ-_"),
            "abc123XYZ-_",
        )


if __name__ == "__main__":
    unittest.main()
