from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import media_publisher.runtime_env as runtime_env


class RuntimeEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime_env.INITIAL_CREDENTIAL_JSON.clear()

    def test_materialize_credentials_writes_canva_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            payload = '{"refresh_token": "from-env"}'
            with patch.dict(os.environ, {"CANVA_TOKEN_JSON": payload}, clear=False):
                written = runtime_env.materialize_credentials(root)

            destination = root / runtime_env.CANVA_TOKEN_RELATIVE_PATH
            self.assertEqual(written, [destination])
            self.assertEqual(destination.read_text(encoding="utf-8"), payload)

    def test_maybe_persist_canva_token_skips_without_sync_pat(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            token_path = root / runtime_env.CANVA_TOKEN_RELATIVE_PATH
            token_path.parent.mkdir(parents=True)
            token_path.write_text('{"refresh_token": "new"}', encoding="utf-8")
            runtime_env.CANVA_TOKEN_BASELINE = '{"refresh_token": "old"}'

            with patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(runtime_env.maybe_persist_canva_token(root))

    def test_maybe_persist_canva_token_updates_secret_when_rotated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            token_path = root / runtime_env.CANVA_TOKEN_RELATIVE_PATH
            token_path.parent.mkdir(parents=True)
            token_path.write_text('{"refresh_token": "new"}', encoding="utf-8")
            runtime_env.CANVA_TOKEN_BASELINE = '{"refresh_token": "old"}'

            with patch.dict(
                os.environ,
                {
                    "CANVA_TOKEN_SYNC_PAT": "pat",
                },
                clear=True,
            ), patch(
                "media_publisher.runtime_env._set_github_actions_secret_file_api"
            ) as api_mock:
                message = runtime_env.maybe_persist_canva_token(root)

            self.assertEqual(
                message,
                "Updated CANVA_TOKEN_JSON GitHub secret after Canva token refresh.",
            )
            api_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
