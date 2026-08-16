from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_publisher.runtime_env import (
    CANVA_TOKEN_BASELINE,
    CANVA_TOKEN_RELATIVE_PATH,
    CREDENTIAL_ENV_FILES,
    DAILY_PLAYLIST_SLOTS_RELATIVE_PATH,
    INITIAL_CREDENTIAL_JSON,
    materialize_credentials,
    maybe_persist_canva_token,
    note_canva_token_baseline,
    parse_github_owner_repo,
    load_publish_timing,
)


class RuntimeEnvTests(unittest.TestCase):
    def test_parse_github_owner_repo(self) -> None:
        self.assertEqual(
            parse_github_owner_repo("git@github.com:org/media-publisher.git"),
            "org/media-publisher",
        )
        self.assertEqual(
            parse_github_owner_repo("https://github.com/org/media-publisher.git"),
            "org/media-publisher",
        )
        self.assertIsNone(parse_github_owner_repo("https://example.com/org/repo.git"))

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

    def test_materialize_daily_playlist_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            payload = '{"playlist_id":"PLdaily","quote":"q1","reel":"r1","lau":"l1"}'
            with patch.dict(
                os.environ,
                {"YOUTUBE_DAILY_PLAYLIST_JSON": payload},
                clear=False,
            ):
                materialize_credentials(root)
            destination = root / DAILY_PLAYLIST_SLOTS_RELATIVE_PATH
            written = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(written["playlist_id"], "PLdaily")
            self.assertEqual(written["quote"], "q1")

    def test_load_publish_timing(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PUBLISH_JSON": (
                    '{"timezone":"Europe/Sofia","quotes_hour":8,"videos_hour":18}'
                )
            },
            clear=False,
        ):
            timing = load_publish_timing()
        self.assertEqual(timing.timezone, "Europe/Sofia")
        self.assertEqual(timing.quotes_hour, 8)
        self.assertEqual(timing.videos_hour, 18)

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
                    "CONFIG_SYNC_PAT": "pat",
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
            note_canva_token_baseline(root)
            import media_publisher.runtime_env as runtime_env

            runtime_env.CANVA_TOKEN_BASELINE = '{"refresh_token": "old"}'

            with patch.dict(
                os.environ,
                {
                    "CONFIG_SYNC_PAT": "pat",
                    "GITHUB_REPOSITORY": "owner/repo",
                },
                clear=True,
            ), patch(
                "media_publisher.runtime_env._set_github_actions_secret_file_api"
            ) as api_mock:
                message = maybe_persist_canva_token(root)

            self.assertEqual(
                message,
                "Updated CANVA_TOKEN_JSON GitHub secret after Canva token refresh.",
            )
            api_mock.assert_called_once()
            self.assertEqual(
                api_mock.call_args.kwargs["token"],
                "pat",
            )
            self.assertEqual(
                api_mock.call_args.args[0],
                "owner/repo",
            )

    def test_maybe_persist_canva_token_skips_without_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            token_path = root / CANVA_TOKEN_RELATIVE_PATH
            token_path.parent.mkdir(parents=True)
            token_path.write_text('{"refresh_token": "new"}', encoding="utf-8")
            import media_publisher.runtime_env as runtime_env

            runtime_env.CANVA_TOKEN_BASELINE = '{"refresh_token": "old"}'

            with patch.dict(
                os.environ,
                {"CONFIG_SYNC_PAT": "pat"},
                clear=True,
            ), patch(
                "media_publisher.runtime_env._github_repository_from_git_origin",
                return_value=None,
            ), patch(
                "media_publisher.runtime_env._set_github_actions_secret_file_api"
            ) as api_mock:
                self.assertIsNone(maybe_persist_canva_token(root))
            api_mock.assert_not_called()

    def test_maybe_persist_canva_token_uses_git_origin_when_env_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            token_path = root / CANVA_TOKEN_RELATIVE_PATH
            token_path.parent.mkdir(parents=True)
            token_path.write_text('{"refresh_token": "new"}', encoding="utf-8")
            import media_publisher.runtime_env as runtime_env

            runtime_env.CANVA_TOKEN_BASELINE = '{"refresh_token": "old"}'

            with patch.dict(
                os.environ,
                {"CONFIG_SYNC_PAT": "pat"},
                clear=True,
            ), patch(
                "media_publisher.runtime_env._github_repository_from_git_origin",
                return_value="copied/media-publisher",
            ), patch(
                "media_publisher.runtime_env._set_github_actions_secret_file_api"
            ) as api_mock:
                maybe_persist_canva_token(root)

            self.assertEqual(api_mock.call_args.args[0], "copied/media-publisher")

    def test_github_sync_pat_reads_config_sync_pat(self) -> None:
        with patch.dict(
            os.environ,
            {"CONFIG_SYNC_PAT": "new-pat"},
            clear=True,
        ):
            from media_publisher.runtime_env import github_sync_pat

            self.assertEqual(github_sync_pat(), "new-pat")

    def test_materialize_credentials_keeps_newer_canva_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            token_path = root / CANVA_TOKEN_RELATIVE_PATH
            token_path.parent.mkdir(parents=True)
            refreshed = (
                '{"access_token":"new","refresh_token":"rt-new","expires_at":2000}'
            )
            stale = '{"access_token":"old","refresh_token":"rt-old","expires_at":1000}'
            token_path.write_text(refreshed, encoding="utf-8")

            with patch.dict(
                os.environ,
                {"CANVA_TOKEN_JSON": stale},
                clear=False,
            ):
                written = materialize_credentials(root)

            self.assertEqual(written, [])
            self.assertEqual(token_path.read_text(encoding="utf-8"), refreshed)
            import media_publisher.runtime_env as runtime_env

            # Baseline stays the stale secret so maybe_persist can sync the newer file.
            self.assertEqual(runtime_env.CANVA_TOKEN_BASELINE, stale)

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
                "AIRTABLE_TOKEN=pat\nAIRTABLE_URL=https://airtable.com/appTestBase01/tblTestTable01\n",
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
                for key in (
                    "AIRTABLE_URL",
                    "AIRTABLE_BASE_ID",
                    "AIRTABLE_TABLE_NAME",
                    "AIRTABLE_TOKEN",
                ):
                    os.environ.pop(key, None)
                settings = load_settings(root)

            self.assertEqual(settings.airtable_base_id, "appTestBase01")
            self.assertEqual(settings.airtable_table_name, "tblTestTable01")
            self.assertTrue((root / settings.youtube_token).exists())
            self.assertIn("from-env", (root / settings.youtube_token).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
