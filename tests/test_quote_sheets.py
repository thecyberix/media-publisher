from __future__ import annotations

import unittest

from media_publisher.sources.google_sheets import format_sheet_tab_title


class QuoteSheetTabTitleTests(unittest.TestCase):
    def test_format_sheet_tab_title(self) -> None:
        self.assertEqual(format_sheet_tab_title(2026, 7), "Jul 2026")
        self.assertEqual(format_sheet_tab_title(2025, 1), "Jan 2025")
        self.assertEqual(format_sheet_tab_title(2026, 12), "Dec 2026")


if __name__ == "__main__":
    unittest.main()
