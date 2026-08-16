from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from media_publisher.languages import (
    LanguageConfigError,
    get_language,
    load_languages,
    selected_language,
)


class LanguageConfigTests(unittest.TestCase):
    def test_loads_bulgarian_months(self) -> None:
        language = get_language("bg")
        self.assertIsNotNone(language)
        assert language is not None
        self.assertEqual(language.name, "Bulgarian")
        self.assertEqual(language.month_name(9), "септември")
        self.assertEqual(language.month_number("Юли"), 7)
        self.assertEqual(get_language("Bulgarian"), language)
        language_by_name = selected_language()
        self.assertEqual(language_by_name.alias, "bg")
        self.assertEqual(language_by_name.country, "България")
        events = language_by_name.require_events()
        self.assertEqual(events.program_word, "Програма")
        self.assertEqual(events.page_heading, "Събития")
        ingest = language_by_name.require_ingest()
        self.assertEqual(ingest.smartcat_language_id, 1026)
        self.assertIn("bg", ingest.aliases)
        self.assertEqual(ingest.quote_open, "„")
        publish = language_by_name.require_publish()
        self.assertEqual(publish.hashtag, "#Садгуру")
        self.assertEqual(publish.display_name, "Садгуру")

    def test_selected_language_requires_target_language(self) -> None:
        import os
        from unittest.mock import patch

        env = {key: value for key, value in os.environ.items() if key != "TARGET_LANGUAGE"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(LanguageConfigError):
                selected_language()

    def test_rejects_incomplete_month_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "languages.json"
            path.write_text(
                json.dumps(
                    {
                        "Test": {
                            "alias": "xx",
                            "country": "Nowhere",
                            "months": ["one"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(LanguageConfigError):
                load_languages(str(path))


if __name__ == "__main__":
    unittest.main()
