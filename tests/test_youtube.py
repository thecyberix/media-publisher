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
    _video_is_ready_for_thumbnail,
    build_video_body,
    build_video_status,
    format_publish_at,
    load_client_secrets,
    parse_channel_handle,
    prepare_youtube_thumbnail,
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
        self.assertEqual(
            status,
            {"privacyStatus": "unlisted", "containsSyntheticMedia": False},
        )

    def test_build_video_status_declares_no_synthetic_media_when_scheduled(self) -> None:
        publish_at = datetime.now(timezone.utc) + timedelta(hours=2)
        job = PublishJob(title="Demo", publish_at=publish_at)
        status = build_video_status(job)
        self.assertFalse(status["containsSyntheticMedia"])
        self.assertEqual(status["privacyStatus"], "private")

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

    def test_video_is_ready_for_thumbnail_when_processed(self) -> None:
        item = {"status": {"uploadStatus": "processed"}}
        self.assertTrue(_video_is_ready_for_thumbnail(item))

    def test_video_is_ready_for_thumbnail_when_processing_succeeded(self) -> None:
        item = {"processingDetails": {"processingStatus": "succeeded"}}
        self.assertTrue(_video_is_ready_for_thumbnail(item))

    def test_thumbnail_retryable_error_detects_processing(self) -> None:
        from media_publisher.publishers.youtube import _thumbnail_retryable_error

        self.assertTrue(
            _thumbnail_retryable_error(
                "The video has not been processed yet. Please wait before uploading."
            )
        )


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
        self.assertFalse(body["status"]["containsSyntheticMedia"])

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

    def test_set_thumbnail_waits_for_processing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            secrets_path = self._write_client_secrets(root)
            token_path = root / "token.json"
            thumb = root / "cover.png"
            thumb.write_bytes(b"png")
            save_token(
                token_path,
                YouTubeToken(
                    access_token="access",
                    refresh_token="refresh",
                    expires_at=9999999999.0,
                ),
            )
            client = YouTubeClient(secrets_path, token_path)
            with patch.object(client, "wait_for_video_ready_for_thumbnail") as wait_mock:
                with patch.object(
                    client,
                    "_request",
                    return_value=(200, {}, b""),
                ):
                    client.set_thumbnail("vid123", thumb)

        wait_mock.assert_called_once_with("vid123")

    def test_publish_to_youtube_prepends_short_cover_intro(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            secrets_path = self._write_client_secrets(root)
            token_path = root / "token.json"
            video_path = root / "clip.mp4"
            thumb_path = root / "cover.png"
            baked_path = root / "clip.youtube-short-cover-intro.mp4"
            video_path.write_bytes(b"video")
            thumb_path.write_bytes(b"png")
            prepared = root / "cover.youtube-thumb.jpg"
            prepared.write_bytes(b"jpeg")
            baked_path.write_bytes(b"baked")
            job = PublishJob(
                title="Clip",
                video_path=str(video_path),
                thumbnail_path=str(thumb_path),
                video_format="short_form",
            )
            with patch(
                "media_publisher.publishers.youtube.prepare_youtube_thumbnail",
                return_value=prepared,
            ):
                with patch(
                    "media_publisher.publishers.youtube.ensure_short_with_cover_intro",
                    return_value=baked_path,
                ) as cover_mock:
                    with patch("media_publisher.publishers.youtube.YouTubeClient") as client_cls:
                        client_cls.return_value.upload_video.return_value = "vid123"
                        video_id = publish_to_youtube(
                            job,
                            client_secrets_path=secrets_path,
                            token_path=token_path,
                        )

        self.assertEqual(video_id, "vid123")
        cover_mock.assert_called_once_with(
            video_path,
            prepared,
            ffmpeg_path=None,
            intro_seconds=5.0,
        )
        upload_path = client_cls.return_value.upload_video.call_args.args[0]
        self.assertEqual(upload_path, baked_path)

    def test_publish_to_youtube_sets_short_thumbnail_via_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            secrets_path = self._write_client_secrets(root)
            token_path = root / "token.json"
            video_path = root / "clip.mp4"
            thumb_path = root / "cover.png"
            video_path.write_bytes(b"video")
            thumb_path.write_bytes(b"png")
            prepared = root / "cover.youtube-thumb.jpg"
            prepared.write_bytes(b"jpeg")
            job = PublishJob(
                title="Clip",
                video_path=str(video_path),
                thumbnail_path=str(thumb_path),
                video_format="short_form",
            )
            with patch(
                "media_publisher.publishers.youtube.prepare_youtube_thumbnail",
                return_value=prepared,
            ):
                with patch(
                    "media_publisher.publishers.youtube.ensure_short_with_cover_intro",
                    return_value=video_path,
                ):
                    with patch("media_publisher.publishers.youtube.YouTubeClient") as client_cls:
                        client_cls.return_value.upload_video.return_value = "vid123"
                        video_id = publish_to_youtube(
                            job,
                            client_secrets_path=secrets_path,
                            token_path=token_path,
                        )

        self.assertEqual(video_id, "vid123")
        upload_path = client_cls.return_value.upload_video.call_args.args[0]
        self.assertEqual(upload_path, video_path)
        client_cls.return_value.set_thumbnail.assert_called_once_with(
            "vid123",
            prepared,
        )

    def test_publish_to_youtube_skips_cover_intro_for_image_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            secrets_path = self._write_client_secrets(root)
            token_path = root / "token.json"
            video_path = root / "clip.mp4"
            thumb_path = root / "cover.png"
            video_path.write_bytes(b"video")
            thumb_path.write_bytes(b"png")
            job = PublishJob(
                title="Quote",
                video_path=str(video_path),
                thumbnail_path=str(thumb_path),
                video_format="short_form",
                content_kind="image",
            )
            with patch(
                "media_publisher.publishers.youtube.ensure_short_with_cover_intro"
            ) as cover_mock:
                with patch(
                    "media_publisher.publishers.youtube.prepare_youtube_thumbnail",
                    return_value=thumb_path,
                ):
                    with patch("media_publisher.publishers.youtube.YouTubeClient") as client_cls:
                        client_cls.return_value.upload_video.return_value = "vid123"
                        publish_to_youtube(
                            job,
                            client_secrets_path=secrets_path,
                            token_path=token_path,
                        )

        cover_mock.assert_not_called()
        upload_path = client_cls.return_value.upload_video.call_args.args[0]
        self.assertEqual(upload_path, video_path)

    def test_publish_to_youtube_sets_prepared_short_thumbnail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            secrets_path = self._write_client_secrets(root)
            token_path = root / "token.json"
            video_path = root / "clip.mp4"
            thumb_path = root / "cover.png"
            video_path.write_bytes(b"video")
            thumb_path.write_bytes(b"png")
            prepared = root / "cover.youtube-thumb.jpg"
            prepared.write_bytes(b"jpeg")
            job = PublishJob(
                title="Clip",
                video_path=str(video_path),
                thumbnail_path=str(thumb_path),
                video_format="post",
            )
            with patch(
                "media_publisher.publishers.youtube.prepare_youtube_thumbnail",
                return_value=prepared,
            ) as prepare_mock:
                with patch("media_publisher.publishers.youtube.YouTubeClient") as client_cls:
                    client_cls.return_value.upload_video.return_value = "vid123"
                    video_id = publish_to_youtube(
                        job,
                        client_secrets_path=secrets_path,
                        token_path=token_path,
                    )

        self.assertEqual(video_id, "vid123")
        prepare_mock.assert_called_once()
        client_cls.return_value.set_thumbnail.assert_called_once_with(
            "vid123",
            prepared,
        )

    def test_publish_to_youtube_adds_video_to_playlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            secrets_path = self._write_client_secrets(root)
            token_path = root / "token.json"
            video_path = root / "clip.mp4"
            video_path.write_bytes(b"video")
            job = PublishJob(
                title="Clip",
                video_path=str(video_path),
                video_format="post",
            )
            with patch("media_publisher.publishers.youtube.YouTubeClient") as client_cls:
                client_cls.return_value.upload_video.return_value = "vid123"
                client_cls.return_value.resolve_playlist_id.return_value = "PLtest123"
                video_id = publish_to_youtube(
                    job,
                    client_secrets_path=secrets_path,
                    token_path=token_path,
                    playlist_id="PLtest123",
                )

        self.assertEqual(video_id, "vid123")
        client_cls.return_value.resolve_playlist_id.assert_called_once_with(
            "Съзнателна Планета",
            playlist_id="PLtest123",
        )
        client_cls.return_value.add_video_to_playlist.assert_called_once_with(
            "vid123",
            "PLtest123",
        )

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
