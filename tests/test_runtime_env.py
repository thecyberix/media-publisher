from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_publisher.runtime_env import (
    CANVA_TOKEN_RELATIVE_PATH,
    CREDENTIAL_ENV_FILES,
    INITIAL_CREDENTIAL_JSON,
    materialize_credentials,
    maybe_persist_canva_token,
)


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
            self.assertEqual(
                INITIAL_CREDENTIAL_JSON[CREDENTIAL_ENV_FILES["YOUTUBE_TOKEN_JSON"]],
                payload,
            )

    def test_maybe_persist_canva_token_skips_without_sync_pat(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            token_path = root / CANVA_TOKEN_RELATIVE_PATH
            token_path.parent.mkdir(parents=True)
            token_path.write_text('{"refresh_token": "new"}', encoding="utf-8")
            INITIAL_CREDENTIAL_JSON[CANVA_TOKEN_RELATIVE_PATH] = '{"refresh_token": "old"}'

            with patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(maybe_persist_canva_token(root))

    def test_maybe_persist_canva_token_skips_when_unchanged(self) -> None:
        payload = '{"refresh_token": "same"}'
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            token_path = root / CANVA_TOKEN_RELATIVE_PATH
            token_path.parent.mkdir(parents=True)
            token_path.write_text(payload, encoding="utf-8")
            INITIAL_CREDENTIAL_JSON[CANVA_TOKEN_RELATIVE_PATH] = payload

            with patch.dict(
                os.environ,
                {
                    "CANVA_TOKEN_SYNC_PAT": "pat",
                    "GITHUB_REPOSITORY": "owner/repo",
                },
                clear=True,
            ), patch("media_publisher.runtime_env.subprocess.run") as run_mock:
                self.assertIsNone(maybe_persist_canva_token(root))
            run_mock.assert_not_called()

    def test_maybe_persist_canva_token_updates_secret_when_rotated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            token_path = root / CANVA_TOKEN_RELATIVE_PATH
            token_path.parent.mkdir(parents=True)
            token_path.write_text('{"refresh_token": "new"}', encoding="utf-8")
            INITIAL_CREDENTIAL_JSON[CANVA_TOKEN_RELATIVE_PATH] = '{"refresh_token": "old"}'

            with patch.dict(
                os.environ,
                {
                    "CANVA_TOKEN_SYNC_PAT": "pat",
                    "GITHUB_REPOSITORY": "owner/repo",
                },
                clear=True,
            ), patch("media_publisher.runtime_env.subprocess.run") as run_mock:
                run_mock.return_value.returncode = 0
                run_mock.return_value.stdout = ""
                run_mock.return_value.stderr = ""
                message = maybe_persist_canva_token(root)

            self.assertEqual(
                message,
                "Updated CANVA_TOKEN_JSON GitHub secret after Canva token refresh.",
            )
            run_mock.assert_called_once()
            command = run_mock.call_args.args[0]
            self.assertEqual(command[:4], ["gh", "secret", "set", "CANVA_TOKEN_JSON"])
            self.assertEqual(run_mock.call_args.kwargs["env"]["GH_TOKEN"], "pat")

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
