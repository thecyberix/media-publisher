from __future__ import annotations

import unittest
from pathlib import Path

from media_publisher.config import load_env_file, load_settings


class ConfigTests(unittest.TestCase):
    def test_load_settings_from_env_file(self) -> None:
        env_path = Path(__file__).resolve().parents[1] / ".env.example"
        import os

        for key in list(os.environ):
            if key.startswith("AIRTABLE_"):
                del os.environ[key]

        load_env_file(env_path)
        settings = load_settings(Path(__file__).resolve().parents[1])
        self.assertEqual(settings.airtable_base_id, "appbIH4wzW6ZRUnF5")
        self.assertEqual(settings.airtable_table_name, "tblji1RaFztkeDn04")
        self.assertEqual(settings.airtable_token, "")


if __name__ == "__main__":
    unittest.main()
