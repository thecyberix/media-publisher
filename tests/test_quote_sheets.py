from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from media_publisher.sources.google_sheets import format_sheet_tab_title
from media_publisher.sources.quotes_sheet import load_monthly_quote_texts


class QuoteSheetTabTitleTests(unittest.TestCase):
    def test_format_sheet_tab_title(self) -> None:
        self.assertEqual(format_sheet_tab_title(2026, 7), "Jul 2026")
        self.assertEqual(format_sheet_tab_title(2025, 1), "Jan 2025")
        self.assertEqual(format_sheet_tab_title(2026, 12), "Dec 2026")


class QuotesSourcesConfigStub:
    def __init__(self, *, quotes_sheet: dict) -> None:
        self.path = Path(".")
        self._quotes_sheet = quotes_sheet

    @property
    def quotes_sheet(self) -> dict:
        return self._quotes_sheet


class QuoteSheetTextPriorityTests(unittest.TestCase):
    def _load(self, rows: list[list[str]], *, require_ready: bool = True):
        client = MagicMock()
        client.list_tabs.return_value = [
            SimpleNamespace(sheet_id=1, title="Sep 2026"),
        ]
        client.resolve_sheet_tab_for_month.return_value = SimpleNamespace(
            title="Sep 2026"
        )
        client.get_values.return_value = rows
        config = QuotesSourcesConfigStub(
            quotes_sheet={
                "date_column": "Date",
                "text_bg_column": "Translation",
                "text_en_column": "English",
                "edited_column": "Edited",
                "ready_column": "Ready",
            },
        )
        return load_monthly_quote_texts(
            client,
            config,
            year=2026,
            month=9,
            require_ready=require_ready,
            spreadsheet_id="sheet-id",
        )

    def test_uses_ready_only_when_required(self) -> None:
        quotes = self._load(
            [
                ["Date", "English", "Translation", "Edited", "Ready"],
                ["1 Sep 2026", "EN1", "TR1", "ED1", "RD1"],
                ["2 Sep 2026", "EN2", "TR2", "ED2", ""],
                ["3 Sep 2026", "EN3", "TR3", "", ""],
                ["4 Sep 2026", "EN4", "", "", ""],
            ]
        )
        by_day = {quote.day: quote.text_bg for quote in quotes}
        self.assertEqual(by_day, {1: "RD1"})
        self.assertEqual(quotes[0].text_source, "ready")

    def test_falls_back_to_edited_then_translation_when_ready_empty(self) -> None:
        quotes = self._load(
            [
                ["Date", "English", "Translation", "Edited", "Ready"],
                ["1 Sep 2026", "EN1", "TR1", "ED1", "RD1"],
                ["2 Sep 2026", "EN2", "TR2", "ED2", ""],
                ["3 Sep 2026", "EN3", "TR3", "", ""],
                ["4 Sep 2026", "EN4", "", "", ""],
            ],
            require_ready=False,
        )
        by_day = {quote.day: (quote.text_bg, quote.text_source) for quote in quotes}
        self.assertEqual(
            by_day,
            {
                1: ("RD1", "ready"),
                2: ("ED2", "edited"),
                3: ("TR3", "translation"),
            },
        )

    def test_require_ready_skips_rows_without_ready(self) -> None:
        quotes = self._load(
            [
                ["Date", "English", "Translation", "Edited", "Ready"],
                ["1 Sep 2026", "EN1", "TR1", "ED1", ""],
                ["2 Sep 2026", "EN2", "TR2", "ED2", "RD2"],
            ],
            require_ready=True,
        )
        self.assertEqual([quote.day for quote in quotes], [2])
        self.assertEqual(quotes[0].text_bg, "RD2")

    def test_missing_ready_column_raises(self) -> None:
        from media_publisher.sources.quotes_sheet import QuotesSheetError

        with self.assertRaises(QuotesSheetError) as caught:
            self._load(
                [
                    ["Date", "English", "Translation", "Edited"],
                    ["1 Sep 2026", "EN1", "TR1", "ED1"],
                ]
            )
        self.assertIn("Ready column", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
