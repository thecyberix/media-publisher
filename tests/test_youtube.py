from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from media_publisher.models import PublishJob
from media_publisher.publishers.youtube import (
    YouTubeChannel,
    YouTubeClient,
    YouTubePublishError,
    YouTubeToken,
    build_video_body,
    build_video_status,
    format_publish_at,
    load_client_secrets,
    parse_channel_handle,
    publish_to_youtube,
    save_token,
    validate_schedule_time,
)


class YouTubeHelperTests(unittest.TestCase):
    def test_format_publish_at_uses_utc(self) -> None:
        value = datetime(2026, 7, 4, 12, 30, tzinfo=timezone.utc)
        self.assertEqual(format_publish_at(value), "2026-07-04T12:30:00Z")

    def test_build_video_status_scheduled(self) -> None:
        publish_at = datetime.now(timezone.utc) + timedelta(hours=2)
        job = PublishJob(title="Demo", publish_at=publish_at)
        status = build_video_status(job)
        self.assertEqual(status["privacyStatus"], "private")
        self.assertIn("publishAt", status)

    def test_build_video_status_immediate(self) -> None:
        job = PublishJob(title="Demo", privacy_status="unlisted")
        status = build_video_status(job)
        self.assertEqual(status, {"privacyStatus": "unlisted"})

    def test_load_client_secrets_installed_app(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "client.json"
            path.write_text(
                json.dumps(
                    {
                        "installed": {
                            "client_id": "client-id",
                            "client_secret": "client-secret",
                            "redirect_uris": ["http://127.0.0.1:8766/callback"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            secrets = load_client_secrets(path)
        self.assertEqual(secrets.client_id, "client-id")
        self.assertEqual(secrets.client_secret, "client-secret")
        self.assertEqual(secrets.redirect_uri, "http://127.0.0.1:8766/callback")

    def test_validate_schedule_time_rejects_past(self) -> None:
        publish_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        with self.assertRaises(YouTubePublishError):
            validate_schedule_time(publish_at)

    def test_parse_channel_handle_from_url(self) -> None:
        handle = parse_channel_handle("https://www.youtube.com/@SadhguruBulgarian")
        self.assertEqual(handle, "SadhguruBulgarian")

    def test_parse_channel_handle_from_at_prefix(self) -> None:
        self.assertEqual(parse_channel_handle("@SadhguruBulgarian"), "SadhguruBulgarian")


class YouTubeClientTests(unittest.TestCase):
    def _write_client_secrets(self, directory: Path) -> Path:
        path = directory / "client.json"
        path.write_text(
            json.dumps(
                {
                    "installed": {
                        "client_id": "client-id",
                        "client_secret": "client-secret",
                        "redirect_uris": ["http://127.0.0.1:8766/callback"],
                    }
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_build_video_body_includes_tags(self) -> None:
        publish_at = datetime.now(timezone.utc) + timedelta(hours=3)
        job = PublishJob(
            title="Launch video",
            description="A scheduled upload",
            tags=["launch", "product"],
            publish_at=publish_at,
        )
        body = build_video_body(job)
        self.assertEqual(body["snippet"]["title"], "Launch video")
        self.assertEqual(body["snippet"]["tags"], ["launch", "product"])
        self.assertEqual(body["status"]["privacyStatus"], "private")

    def test_upload_video_returns_video_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            secrets_path = self._write_client_secrets(root)
            token_path = root / "token.json"
            save_token(
                token_path,
                YouTubeToken(
                    access_token="access",
                    refresh_token="refresh",
                    expires_at=9999999999.0,
                ),
            )
            video_path = root / "clip.mp4"
            video_path.write_bytes(b"fake-video")

            client = YouTubeClient(secrets_path, token_path)
            with patch.object(
                client,
                "verify_authorized_channel",
                return_value=YouTubeChannel(
                    id="channel-1",
                    title="Sadhguru Bulgarian",
                    handle="SadhguruBulgarian",
                ),
            ):
                with patch.object(client, "_start_resumable_upload", return_value="https://upload.example/resume") as init_mock:
                    with patch.object(
                        client,
                        "_upload_video_bytes",
                        return_value={"id": "abc123"},
                    ):
                        video_id = client.upload_video(
                            video_path,
                            title="Clip",
                            publish_at=datetime.now(timezone.utc) + timedelta(hours=1),
                        )

        self.assertEqual(video_id, "abc123")
        init_body = init_mock.call_args.kwargs["body"]
        self.assertEqual(init_body["status"]["privacyStatus"], "private")
        self.assertIn("publishAt", init_body["status"])

    def test_verify_authorized_channel_matches_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            secrets_path = self._write_client_secrets(root)
            token_path = root / "token.json"
            save_token(
                token_path,
                YouTubeToken(
                    access_token="access",
                    refresh_token="refresh",
                    expires_at=9999999999.0,
                ),
            )
            client = YouTubeClient(
                secrets_path,
                token_path,
                expected_channel_handle="SadhguruBulgarian",
            )
            with patch.object(
                client,
                "list_my_channels",
                return_value=[
                    YouTubeChannel(
                        id="channel-1",
                        title="Sadhguru Bulgarian",
                        handle="SadhguruBulgarian",
                    )
                ],
            ):
                channel = client.verify_authorized_channel()

        self.assertEqual(channel.handle, "SadhguruBulgarian")

    def test_verify_authorized_channel_rejects_wrong_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            secrets_path = self._write_client_secrets(root)
            token_path = root / "token.json"
            save_token(
                token_path,
                YouTubeToken(
                    access_token="access",
                    refresh_token="refresh",
                    expires_at=9999999999.0,
                ),
            )
            client = YouTubeClient(
                secrets_path,
                token_path,
                expected_channel_handle="SadhguruBulgarian",
            )
            with patch.object(
                client,
                "list_my_channels",
                return_value=[
                    YouTubeChannel(
                        id="channel-2",
                        title="Personal Channel",
                        handle="SomeOtherChannel",
                    )
                ],
            ):
                with patch.object(
                    client,
                    "get_channel_by_handle",
                    return_value=YouTubeChannel(
                        id="channel-1",
                        title="Sadhguru Bulgarian",
                        handle="SadhguruBulgarian",
                    ),
                ):
                    with self.assertRaises(YouTubePublishError):
                        client.verify_authorized_channel()

    def test_publish_to_youtube_requires_video_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            secrets_path = self._write_client_secrets(root)
            token_path = root / "token.json"
            job = PublishJob(title="No file")
            with self.assertRaises(YouTubePublishError):
                publish_to_youtube(
                    job,
                    client_secrets_path=secrets_path,
                    token_path=token_path,
                )


if __name__ == "__main__":
    unittest.main()
