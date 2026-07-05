from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_publisher.runtime_env import CREDENTIAL_ENV_FILES, materialize_credentials


class RuntimeEnvTests(unittest.TestCase):
    def test_materialize_credentials_writes_json_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            payload = '{"access_token": "test"}'
            with patch.dict(
                os.environ,
                {"YOUTUBE_TOKEN_JSON": payload},
                clear=False,
            ):
                written = materialize_credentials(root)

            destination = root / CREDENTIAL_ENV_FILES["YOUTUBE_TOKEN_JSON"]
            self.assertEqual(written, [destination])
            self.assertEqual(destination.read_text(encoding="utf-8"), payload)

    def test_materialize_credentials_skips_unset_env_vars(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            existing = root / "credentials" / "youtube-token.json"
            existing.parent.mkdir(parents=True)
            existing.write_text('{"keep": true}', encoding="utf-8")

            env = {key: "" for key in CREDENTIAL_ENV_FILES}
            with patch.dict(os.environ, env, clear=False):
                written = materialize_credentials(root)

            self.assertEqual(written, [])
            self.assertEqual(existing.read_text(encoding="utf-8"), '{"keep": true}')

    def test_load_settings_materializes_before_reading_paths(self) -> None:
        from media_publisher.config import load_settings

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".env").write_text(
                "AIRTABLE_TOKEN=pat\nAIRTABLE_BASE_ID=app\nAIRTABLE_TABLE_NAME=tbl\n",
                encoding="utf-8",
            )
            token_json = '{"access_token": "from-env", "refresh_token": "r", "expires_at": 9999999999}'
            with patch.dict(
                os.environ,
                {
                    "YOUTUBE_TOKEN_JSON": token_json,
                    "YOUTUBE_CLIENT_SECRETS_JSON": '{"installed":{"client_id":"id","client_secret":"sec"}}',
                },
                clear=False,
            ):
                settings = load_settings(root)

            self.assertTrue((root / settings.youtube_token).exists())
            self.assertIn("from-env", (root / settings.youtube_token).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
