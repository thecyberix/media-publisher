from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_publisher.sources.quotes_config import (
    QuotesConfigError,
    extract_spreadsheet_id,
    load_quotes_sources_config,
)


class TranslatedQuotesUrlTests(unittest.TestCase):
    def test_extract_spreadsheet_id_from_url_and_raw_id(self) -> None:
        self.assertEqual(
            extract_spreadsheet_id(
                "https://docs.google.com/spreadsheets/d/13Hj-v3bGVLs49ZutLx-LwQrcqUXoMcjIDNRP5h0Qmec/edit"
            ),
            "13Hj-v3bGVLs49ZutLx-LwQrcqUXoMcjIDNRP5h0Qmec",
        )
        self.assertEqual(
            extract_spreadsheet_id("13Hj-v3bGVLs49ZutLx-LwQrcqUXoMcjIDNRP5h0Qmec"),
            "13Hj-v3bGVLs49ZutLx-LwQrcqUXoMcjIDNRP5h0Qmec",
        )

    def test_load_quotes_sources_config_applies_translated_quotes_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "quotes_sources.json"
            path.write_text(
                json.dumps(
                    {
                        "quotes_sheet": {
                            "sheet_title_pattern": "{month_abbr} {year}",
                            "text_bg_column": "Translation",
                        },
                        "backgrounds_drive": {"variants": {}},
                        "canva_templates": {},
                        "renders": {},
                    }
                ),
                encoding="utf-8",
            )
            url = (
                "https://docs.google.com/spreadsheets/d/"
                "13Hj-v3bGVLs49ZutLx-LwQrcqUXoMcjIDNRP5h0Qmec/edit"
            )
            with patch.dict(os.environ, {"TRANSLATED_QUOTES_URL": url}, clear=False):
                config = load_quotes_sources_config(path)
            self.assertEqual(
                config.spreadsheet_id, "13Hj-v3bGVLs49ZutLx-LwQrcqUXoMcjIDNRP5h0Qmec"
            )

    def test_spreadsheet_id_requires_translated_quotes_url(self) -> None:
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
            with patch.dict(os.environ, {"TRANSLATED_QUOTES_URL": ""}, clear=False):
                config = load_quotes_sources_config(path)
                with self.assertRaises(QuotesConfigError):
                    _ = config.spreadsheet_id


if __name__ == "__main__":
    unittest.main()
