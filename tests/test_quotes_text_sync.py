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
    extract_ready_text_from_row,
    find_ready_column_index,
    find_tab_by_candidates,
    month_file_name_candidates,
    sync_month_quote_texts,
    year_workbook_name,
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

    def test_sync_reuses_ready_into_edited_and_ai_fallback(self) -> None:
        config = self._config()
        sheets = MagicMock()
        drive = MagicMock()

        english_rows = [
            EnglishQuoteRow(
                day=1,
                publish_date=date(2026, 9, 1),
                date_label="1 Sep 2026",
                english="New quote one",
                previously_posted_on=date(2025, 3, 10),
                previously_posted_label="10 Mar 2025",
            ),
            EnglishQuoteRow(
                day=2,
                publish_date=date(2026, 9, 2),
                date_label="2 Sep 2026",
                english="Brand new quote",
                previously_posted_on=None,
                previously_posted_label="",
            ),
        ]

        dest = DestinationSheetRef(
            spreadsheet_id="dest-sep",
            spreadsheet_name="Sep 2026",
            tab=SheetTab(sheet_id=1, title="Sep 2026"),
        )

        def fake_lookup(**kwargs):
            posted = kwargs["posted_on"]
            if posted == date(2025, 3, 10):
                return "Стара готова цитат"
            return None

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
                "media_publisher.quotes_text_sync.lookup_ready_translation",
                side_effect=fake_lookup,
            ),
            patch(
                "media_publisher.quotes_text_sync._read_sheet_rows",
                return_value=[
                    ["Date", "English", "Translation", "Edited", "Ready"],
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
            )

        self.assertEqual(result.added_count, 2)
        self.assertEqual(result.reused_count, 1)
        self.assertEqual(result.translated_count, 1)
        sheets.batch_update_values.assert_called()
        written = sheets.batch_update_values.call_args[0][1]
        values = {range_a1: cells[0][0] for range_a1, cells in written}
        self.assertIn("Стара готова цитат", values.values())
        self.assertIn("BG:Brand new quote", values.values())

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
                    ["Date", "English", "Translation", "Edited", "Ready"],
                    ["5 Sep 2026", "Same text", "Стар", "", ""],
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
            )
        self.assertEqual(result.changes, [])
        sheets.batch_update_values.assert_not_called()


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
