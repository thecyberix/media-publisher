from __future__ import annotations

import unittest
from pathlib import Path

from media_publisher.config import load_env_file, load_settings


class ConfigTests(unittest.TestCase):
    def test_load_settings_from_env_file(self) -> None:
        env_path = Path(__file__).resolve().parents[1] / ".env.example"
        import os

        for key in (
            "AIRTABLE_TOKEN",
            "AIRTABLE_URL",
            "AIRTABLE_BASE_ID",
            "AIRTABLE_TABLE_NAME",
            "YOUTUBE_CHANNEL_HANDLE",
            "META_PAGE_USERNAME",
            "META_INSTAGRAM_USERNAME",
            "TARGET_LANGUAGE",
            "TARGET_LANGUAGE_NAME",
            "TARGET_COUNTRY",
        ):
            os.environ.pop(key, None)

        load_env_file(env_path)
        settings = load_settings(Path(__file__).resolve().parents[1])
        self.assertEqual(settings.airtable_base_id, "appbIH4wzW6ZRUnF5")
        self.assertEqual(settings.airtable_table_name, "tblji1RaFztkeDn04")
        self.assertEqual(settings.airtable_token, "")
        self.assertEqual(settings.youtube_channel_handle, "SadhguruBulgarian")
        self.assertEqual(settings.meta_page_username, "SadhguruBulgarian")
        self.assertEqual(settings.meta_instagram_username, "sadhguru.bulgarian")
        self.assertEqual(settings.target_language, "bg")
        self.assertEqual(settings.target_language_name, "Bulgarian")
        self.assertEqual(settings.target_country, "България")


if __name__ == "__main__":
    unittest.main()
