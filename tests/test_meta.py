from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from media_publisher.models import PublishJob
from media_publisher.publishers.facebook import FacebookPublishError, publish_to_facebook
from media_publisher.publishers.instagram import InstagramPublishError, publish_to_instagram
from media_publisher.publishers.meta import (
    FacebookHostingAsset,
    MetaClient,
    MetaError,
    extract_facebook_video_id,
    normalize_facebook_page_username,
    normalize_facebook_permalink,
    normalize_instagram_username,
    resolve_permanent_page_token,
    validate_publish_at,
)
from media_publisher.sources.airtable import (
    AirtableRecord,
    DEFAULT_PUBLISH_TIMEZONE,
    FIELD_TITLE,
    record_schedule_tasks,
)


class PublishAtValidationTests(unittest.TestCase):
    def test_validate_publish_at_requires_lead_time(self) -> None:
        now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
        too_soon = now + timedelta(minutes=5)
        with self.assertRaises(MetaError):
            validate_publish_at(too_soon, now=now)

    def test_validate_publish_at_accepts_valid_window(self) -> None:
        now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
        scheduled = now + timedelta(hours=2)
        self.assertEqual(validate_publish_at(scheduled, now=now), scheduled)


class AirtablePublishAtTests(unittest.TestCase):
    def test_record_schedule_tasks_maps_platform_date(self) -> None:
        tasks = record_schedule_tasks(
            AirtableRecord(
                id="recABC",
                fields={
                    "Status": "5. Synchronization done",
                    FIELD_TITLE: "Sample Title",
                    "Video name translated": "Преведено заглавие",
                    "SG-YT-Date published": "2026-07-05",
                },
            )
        )
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].platform, "youtube")
        self.assertEqual(tasks[0].publish_at.date().isoformat(), "2026-07-05")
        self.assertEqual(
            tasks[0].publish_at.astimezone(ZoneInfo(DEFAULT_PUBLISH_TIMEZONE)).hour,
            18,
        )


class MetaTokenSetupTests(unittest.TestCase):
    def test_resolve_permanent_page_token_exchanges_user_token(self) -> None:
        with patch(
            "media_publisher.publishers.meta.inspect_access_token",
            return_value=type(
                "Info",
                (),
                {
                    "token_type": "USER",
                    "is_valid": True,
                    "expires_at": None,
                    "scopes": ("pages_manage_posts",),
                },
            )(),
        ), patch(
            "media_publisher.publishers.meta.exchange_short_lived_user_token",
            return_value="long-lived-user-token",
        ) as exchange_mock, patch(
            "media_publisher.publishers.meta.list_managed_page_credentials",
            return_value=[
                type(
                    "Page",
                    (),
                    {
                        "page_id": "page_123",
                        "name": "Sadhguru Bulgarian",
                        "username": "SadhguruBulgarian",
                        "instagram_account_id": "ig_456",
                        "instagram_username": "sadhguru.bulgarian",
                        "access_token": "permanent-page-token",
                    },
                )()
            ],
        ):
            credentials = resolve_permanent_page_token(
                "user-token",
                page_username="SadhguruBulgarian",
                app_id="app123",
                app_secret="secret",
            )

        exchange_mock.assert_called_once()
        self.assertEqual(credentials.access_token, "permanent-page-token")

    def test_resolve_permanent_page_token_from_user_token(self) -> None:
        with patch(
            "media_publisher.publishers.meta.inspect_access_token",
            return_value=type(
                "Info",
                (),
                {
                    "token_type": "USER",
                    "is_valid": True,
                    "expires_at": None,
                    "scopes": ("pages_manage_posts",),
                },
            )(),
        ), patch(
            "media_publisher.publishers.meta.exchange_short_lived_user_token",
            return_value="long-lived-user-token",
        ), patch(
            "media_publisher.publishers.meta.list_managed_page_credentials",
            return_value=[
                type(
                    "Page",
                    (),
                    {
                        "page_id": "page_123",
                        "name": "Sadhguru Bulgarian",
                        "username": "SadhguruBulgarian",
                        "instagram_account_id": "ig_456",
                        "instagram_username": "sadhguru.bulgarian",
                        "access_token": "permanent-page-token",
                    },
                )()
            ],
        ):
            credentials = resolve_permanent_page_token(
                "user-token",
                page_username="SadhguruBulgarian",
                app_id="app123",
                app_secret="secret",
            )

        self.assertEqual(credentials.access_token, "permanent-page-token")
        self.assertEqual(credentials.page_id, "page_123")
        self.assertEqual(credentials.instagram_account_id, "ig_456")


class FacebookVideoIdExtractionTests(unittest.TestCase):
    def test_extract_facebook_video_id_from_reel_url(self) -> None:
        self.assertEqual(
            extract_facebook_video_id("https://www.facebook.com/reel/4025341661101246/"),
            "4025341661101246",
        )

    def test_extract_facebook_video_id_from_numeric(self) -> None:
        self.assertEqual(extract_facebook_video_id("4025341661101246"), "4025341661101246")

    def test_extract_facebook_video_id_rejects_garbage(self) -> None:
        self.assertIsNone(extract_facebook_video_id("not-a-video"))


class UsernameNormalizationTests(unittest.TestCase):
    def test_normalize_facebook_page_username_from_url(self) -> None:
        self.assertEqual(
            normalize_facebook_page_username("https://www.facebook.com/SadhguruBulgarian"),
            "SadhguruBulgarian",
        )

    def test_normalize_instagram_username_from_url(self) -> None:
        self.assertEqual(
            normalize_instagram_username("https://www.instagram.com/sadhguru.bulgarian/"),
            "sadhguru.bulgarian",
        )

    def test_normalize_facebook_permalink_from_relative_reel_path(self) -> None:
        self.assertEqual(
            normalize_facebook_permalink("/reel/4025341661101246/"),
            "https://www.facebook.com/reel/4025341661101246/",
        )

    def test_normalize_facebook_permalink_keeps_absolute_url(self) -> None:
        url = "https://www.facebook.com/reel/4025341661101246/"
        self.assertEqual(normalize_facebook_permalink(url), url)


class MetaClientTests(unittest.TestCase):
    def test_resolve_page_by_username(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        with patch.object(client, "test_connection") as connection_mock:
            connection_mock.return_value = {
                "id": "page_123",
                "name": "Sadhguru Bulgarian",
                "username": "SadhguruBulgarian",
                "instagram_business_account": {
                    "id": "ig_456",
                    "username": "sadhguru.bulgarian",
                },
            }
            page_info = client.resolve_page_by_username("SadhguruBulgarian")

        self.assertEqual(page_info.page_id, "page_123")
        self.assertEqual(page_info.instagram_account_id, "ig_456")
        self.assertEqual(page_info.instagram_username, "sadhguru.bulgarian")

    def test_verify_instagram_username_rejects_mismatch(self) -> None:
        client = MetaClient("token-test")
        page_info = type(
            "PageInfo",
            (),
            {
                "page_id": "page_123",
                "name": "Sadhguru Bulgarian",
                "username": "SadhguruBulgarian",
                "instagram_account_id": "ig_456",
                "instagram_username": "other.account",
            },
        )()
        with self.assertRaises(MetaError):
            client.verify_instagram_username(page_info, "sadhguru.bulgarian")

    def test_get_facebook_video_permalink_normalizes_relative_reel_path(self) -> None:
        client = MetaClient("token-test")
        with patch.object(
            client,
            "_request",
            return_value={"permalink_url": "/reel/4025341661101246/"},
        ):
            permalink = client.get_facebook_video_permalink("4025341661101246")

        self.assertEqual(
            permalink,
            "https://www.facebook.com/reel/4025341661101246/",
        )

    def test_schedule_facebook_video_with_url(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
        publish_at = now + timedelta(hours=2)
        with patch.object(client, "_multipart_request") as request_mock, patch(
            "media_publisher.publishers.meta.validate_publish_at",
            side_effect=lambda value, **kwargs: validate_publish_at(value, now=now),
        ):
            request_mock.return_value = {"id": "fb_video_1"}
            video_id = client.schedule_facebook_video(
                page_id="page123",
                title="Launch video",
                description="Details",
                video_url="https://cdn.example.com/video.mp4",
                publish_at=publish_at,
            )

        self.assertEqual(video_id, "fb_video_1")
        fields = request_mock.call_args.kwargs["fields"]
        self.assertEqual(fields["file_url"], "https://cdn.example.com/video.mp4")
        self.assertEqual(fields["published"], "false")
        self.assertIn("scheduled_publish_time", fields)

    def test_schedule_facebook_video_uses_chunked_upload_for_local_file(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        video_path = Path("launch.mp4")
        with patch.object(
            client,
            "_upload_page_video_chunked",
            return_value="fb_video_1",
        ) as upload_mock:
            video_id = client.schedule_facebook_video(
                page_id="page123",
                title="Launch video",
                description="Details",
                video_path=video_path,
            )

        self.assertEqual(video_id, "fb_video_1")
        upload_mock.assert_called_once()
        finish_fields = upload_mock.call_args.kwargs["finish_fields"]
        self.assertEqual(finish_fields["title"], "Launch video")
        self.assertEqual(finish_fields["description"], "Details")
        self.assertEqual(finish_fields["published"], "true")

    def test_schedule_facebook_video_sets_thumbnail(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        thumb = Path("cover.png")
        with (
            patch.object(client, "_upload_page_video_chunked", return_value="fb_video_1"),
            patch.object(client, "set_facebook_video_thumbnail") as thumb_mock,
        ):
            video_id = client.schedule_facebook_video(
                page_id="page123",
                title="Launch video",
                description="Details",
                video_path=Path("launch.mp4"),
                thumbnail_path=thumb,
            )

        self.assertEqual(video_id, "fb_video_1")
        thumb_mock.assert_called_once_with("fb_video_1", thumb)

    def test_schedule_instagram_reel_publishes_container(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        with (
            patch.object(client, "create_instagram_media_container", return_value="ctr_1"),
            patch.object(
                client,
                "wait_for_container",
                return_value=type(
                    "Status",
                    (),
                    {"id": "ctr_1", "status_code": "FINISHED", "status": None},
                )(),
            ),
            patch.object(client, "publish_instagram_container", return_value="ig_media_1"),
        ):
            media_id = client.schedule_instagram_reel(
                instagram_account_id="ig123",
                caption="Caption",
                video_url="https://cdn.example.com/video.mp4",
            )

        self.assertEqual(media_id, "ig_media_1")

    def test_create_instagram_media_container_trial_reel(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        with patch.object(client, "_request", return_value={"id": "ctr_1"}) as request_mock:
            container_id = client.create_instagram_media_container(
                instagram_account_id="ig123",
                caption="Caption",
                video_url="https://cdn.example.com/video.mp4",
                trial_reel=True,
            )

        self.assertEqual(container_id, "ctr_1")
        body = request_mock.call_args.kwargs["body"]
        self.assertEqual(
            body["trial_params"],
            '{"graduation_strategy": "MANUAL"}',
        )

    def test_schedule_instagram_reel_trial_reel(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        with (
            patch.object(client, "create_instagram_media_container", return_value="ctr_1") as create_mock,
            patch.object(
                client,
                "wait_for_container",
                return_value=type(
                    "Status",
                    (),
                    {"id": "ctr_1", "status_code": "FINISHED", "status": None},
                )(),
            ),
            patch.object(client, "publish_instagram_container", return_value="ig_media_1"),
        ):
            media_id = client.schedule_instagram_reel(
                instagram_account_id="ig123",
                caption="Caption",
                video_url="https://cdn.example.com/video.mp4",
                trial_reel=True,
            )

        self.assertEqual(media_id, "ig_media_1")
        self.assertTrue(create_mock.call_args.kwargs["trial_reel"])

    def test_schedule_instagram_feed_video_uses_reels_path(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        with patch.object(client, "schedule_instagram_reel", return_value="ig_media_1") as reel_mock:
            media_id = client.schedule_instagram_feed_video(
                instagram_account_id="ig123",
                caption="Caption",
                video_url="https://cdn.example.com/video.mp4",
            )

        self.assertEqual(media_id, "ig_media_1")
        reel_mock.assert_called_once_with(
            instagram_account_id="ig123",
            caption="Caption",
            video_url="https://cdn.example.com/video.mp4",
            publish_at=None,
        )

    def test_create_instagram_media_container_defaults_to_reels(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        with patch.object(client, "_request", return_value={"id": "ctr_1"}) as request_mock:
            client.create_instagram_media_container(
                instagram_account_id="ig123",
                caption="Caption",
                video_url="https://cdn.example.com/video.mp4",
            )

        body = request_mock.call_args.kwargs["body"]
        self.assertEqual(body["media_type"], "REELS")
        self.assertEqual(body["video_url"], "https://cdn.example.com/video.mp4")

    def test_schedule_instagram_reel_uses_instagram_resumable_upload(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        video_path = Path("quote.mp4")
        with (
            patch.object(
                client,
                "create_instagram_resumable_reel_container",
                return_value=("ctr_1", "https://rupload.facebook.com/ig-api-upload/v21.0/ctr_1"),
            ) as create_mock,
            patch.object(client, "upload_instagram_resumable_video") as upload_mock,
            patch.object(
                client,
                "wait_for_container",
                return_value=type(
                    "Status",
                    (),
                    {"id": "ctr_1", "status_code": "FINISHED", "status": None},
                )(),
            ),
            patch.object(client, "publish_instagram_container", return_value="ig_media_1"),
        ):
            media_id = client.schedule_instagram_reel(
                instagram_account_id="ig123",
                caption="Caption",
                video_path=video_path,
                page_id="page123",
                trial_reel=True,
            )

        self.assertEqual(media_id, "ig_media_1")
        create_mock.assert_called_once()
        upload_mock.assert_called_once_with(
            container_id="ctr_1",
            video_path=video_path,
            upload_uri="https://rupload.facebook.com/ig-api-upload/v21.0/ctr_1",
        )

    def test_schedule_instagram_reel_hosts_large_local_video(self) -> None:
        from media_publisher.video_duration import INSTAGRAM_SINGLE_UPLOAD_MAX_BYTES

        client = MetaClient("token-test", app_id="app123")
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "large.mp4"
            video_path.write_bytes(b"x" * (INSTAGRAM_SINGLE_UPLOAD_MAX_BYTES + 1))
            with (
                patch.object(
                    client,
                    "upload_unpublished_video_url",
                    return_value=type(
                        "Asset",
                        (),
                        {
                            "asset_id": "fb_video_1",
                            "url": "https://cdn.example.com/hosted.mp4",
                            "kind": "video",
                        },
                    )(),
                ) as host_mock,
                patch.object(
                    client,
                    "create_instagram_media_container",
                    return_value="ctr_1",
                ) as container_mock,
                patch.object(
                    client,
                    "wait_for_container",
                    return_value=type(
                        "Status",
                        (),
                        {"id": "ctr_1", "status_code": "FINISHED", "status": None},
                    )(),
                ),
                patch.object(client, "publish_instagram_container", return_value="ig_media_1"),
                patch.object(client, "upload_instagram_resumable_video") as upload_mock,
            ):
                media_id = client.schedule_instagram_reel(
                    instagram_account_id="ig123",
                    caption="Caption",
                    video_path=video_path,
                    page_id="page123",
                )

        self.assertEqual(media_id, "ig_media_1")
        host_mock.assert_called_once_with("page123", video_path)
        container_mock.assert_called_once()
        self.assertEqual(
            container_mock.call_args.kwargs["video_url"],
            "https://cdn.example.com/hosted.mp4",
        )
        upload_mock.assert_not_called()

    def test_upload_instagram_resumable_video_uploads_full_file_once(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        with tempfile.TemporaryDirectory() as tmpdir:
            video = Path(tmpdir) / "quote.mp4"
            video.write_bytes(b"0123456789")
            with patch.object(client, "_post_instagram_video_file") as upload_mock:
                client.upload_instagram_resumable_video(
                    container_id="ctr_1",
                    video_path=video,
                    upload_uri="https://rupload.example/upload",
                )

            upload_mock.assert_called_once_with(
                "https://rupload.example/upload",
                video_path=video.resolve(),
                file_size=10,
                error_prefix="Meta Instagram resumable upload failed",
                max_attempts=5,
            )

    def test_upload_instagram_resumable_video_uploads_large_file_in_one_request(self) -> None:
        from media_publisher.video_duration import INSTAGRAM_SINGLE_UPLOAD_MAX_BYTES

        client = MetaClient("token-test", app_id="app123")
        with tempfile.TemporaryDirectory() as tmpdir:
            video = Path(tmpdir) / "large.mp4"
            video.write_bytes(b"x" * (INSTAGRAM_SINGLE_UPLOAD_MAX_BYTES + 1))
            with patch.object(client, "_post_instagram_video_file") as upload_mock:
                client.upload_instagram_resumable_video(
                    container_id="ctr_1",
                    video_path=video,
                    upload_uri="https://rupload.example/upload",
                )

            upload_mock.assert_called_once_with(
                "https://rupload.example/upload",
                video_path=video.resolve(),
                file_size=INSTAGRAM_SINGLE_UPLOAD_MAX_BYTES + 1,
                error_prefix="Meta Instagram resumable upload failed",
                max_attempts=5,
            )


class PublisherWrapperTests(unittest.TestCase):
    @staticmethod
    def _passthrough_instagram_video(path: Path, *, ffmpeg_path: str | None = None) -> Path:
        return Path(path)

    def test_publish_to_facebook_requires_video(self) -> None:
        with self.assertRaises(FacebookPublishError):
            publish_to_facebook(
                PublishJob(title="No video"),
                page_id="page123",
                access_token="token",
            )

    def test_publish_to_instagram_requires_video(self) -> None:
        with self.assertRaises(InstagramPublishError):
            publish_to_instagram(
                PublishJob(title="No video"),
                instagram_account_id="ig123",
                access_token="token",
            )

    def test_publish_to_facebook_delegates_to_meta_client(self) -> None:
        job = PublishJob(
            title="Launch",
            video_url="https://cdn.example.com/video.mp4",
        )
        with patch("media_publisher.publishers.facebook.MetaClient") as client_cls:
            client_cls.return_value.schedule_facebook_video.return_value = "fb_video_1"
            post_id = publish_to_facebook(
                job,
                page_id="page123",
                access_token="token",
                app_id="app123",
            )

        self.assertEqual(post_id, "fb_video_1")
        client_cls.assert_called_once_with("token", app_id="app123")
        client_cls.return_value.schedule_facebook_video.assert_called_once()

    def test_publish_to_facebook_uses_reel_for_short_form(self) -> None:
        job = PublishJob(
            title="Launch",
            video_url="https://cdn.example.com/video.mp4",
            video_format="short_form",
        )
        with patch("media_publisher.publishers.facebook.MetaClient") as client_cls:
            client_cls.return_value.schedule_facebook_reel.return_value = "fb_reel_1"
            post_id = publish_to_facebook(job, page_id="page123", access_token="token")

        self.assertEqual(post_id, "fb_reel_1")
        client_cls.return_value.schedule_facebook_reel.assert_called_once()

    def test_publish_to_facebook_never_uses_draft_for_private_privacy_status(self) -> None:
        """YouTube-style privacy_status=private must not make Facebook Reels DRAFT."""
        publish_at = datetime(2026, 7, 16, 15, 0, tzinfo=timezone.utc)
        job = PublishJob(
            title="Launch",
            video_url="https://cdn.example.com/video.mp4",
            video_format="short_form",
            privacy_status="private",
            publish_at=publish_at,
        )
        with patch("media_publisher.publishers.facebook.MetaClient") as client_cls:
            client_cls.return_value.schedule_facebook_reel.return_value = "fb_reel_1"
            publish_to_facebook(job, page_id="page123", access_token="token")

        reel_call = client_cls.return_value.schedule_facebook_reel.call_args
        self.assertEqual(reel_call.kwargs["publish_at"], publish_at)
        self.assertFalse(reel_call.kwargs["unpublished"])

    def test_publish_existing_facebook_reel_sets_published(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        with patch.object(client, "_request", return_value={"success": True}) as request_mock:
            client.publish_existing_facebook_reel(
                page_id="page123",
                video_id="4025341661101246",
                title="Launch",
            )

        request_mock.assert_called_once_with(
            "POST",
            "page123/video_reels",
            body={
                "upload_phase": "finish",
                "video_id": "4025341661101246",
                "video_state": "PUBLISHED",
                "title": "Launch",
            },
        )

    def test_schedule_facebook_reel_schedules_as_public(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
        publish_at = now + timedelta(hours=2)
        with (
            patch.object(
                client,
                "_request",
                side_effect=[
                    {"video_id": "vid_1", "upload_url": "https://upload.example"},
                    {},
                ],
            ) as request_mock,
            patch.object(client, "_upload_facebook_reel_video"),
            patch(
                "media_publisher.publishers.meta.validate_publish_at",
                side_effect=lambda value, **kwargs: validate_publish_at(value, now=now),
            ),
        ):
            video_id = client.schedule_facebook_reel(
                page_id="page123",
                title="Launch",
                description="Details",
                video_path=Path("quote.mp4"),
                publish_at=publish_at,
                unpublished=True,  # ignored when publish_at is set
            )

        self.assertEqual(video_id, "vid_1")
        finish_body = request_mock.call_args_list[1].kwargs["body"]
        self.assertEqual(finish_body["video_state"], "SCHEDULED")
        self.assertIn("scheduled_publish_time", finish_body)

    def test_publish_to_instagram_publishes_long_form_video_with_url(self) -> None:
        job = PublishJob(
            title="Launch",
            description="Caption text",
            video_url="https://cdn.example.com/video.mp4",
            video_format="post",
        )
        with patch("media_publisher.publishers.instagram.MetaClient") as client_cls:
            client_cls.return_value.schedule_instagram_feed_video.return_value = "ig_media_1"
            media_id = publish_to_instagram(
                job,
                instagram_account_id="ig123",
                access_token="token",
            )

        self.assertEqual(media_id, "ig_media_1")
        client_cls.return_value.schedule_instagram_feed_video.assert_called_once()

    def test_publish_to_instagram_publishes_long_form_local_video(self) -> None:
        job = PublishJob(
            title="Launch",
            description="Caption text",
            video_path="downloads/happyscribe/sample.mp4",
            video_format="post",
        )
        with (
            patch("media_publisher.publishers.instagram.MetaClient") as client_cls,
            patch(
                "media_publisher.publishers.instagram.ensure_instagram_upload_video",
                side_effect=self._passthrough_instagram_video,
            ),
        ):
            client_cls.return_value.schedule_instagram_reel.return_value = "ig_media_1"
            media_id = publish_to_instagram(
                job,
                instagram_account_id="ig123",
                access_token="token",
                app_id="app123",
                page_id="page123",
            )

        self.assertEqual(media_id, "ig_media_1")
        client_cls.return_value.schedule_instagram_reel.assert_called_once()

    def test_publish_to_instagram_long_form_local_video_calls_meta_client(self) -> None:
        job = PublishJob(
            title="Launch",
            description="Caption text",
            video_path="downloads/happyscribe/sample.mp4",
            video_format="post",
        )
        with (
            patch("media_publisher.publishers.instagram.MetaClient") as client_cls,
            patch(
                "media_publisher.publishers.instagram.ensure_instagram_upload_video",
                side_effect=self._passthrough_instagram_video,
            ),
        ):
            client_cls.return_value.schedule_instagram_reel.return_value = "ig_media_1"
            publish_to_instagram(
                job,
                instagram_account_id="ig123",
                access_token="token",
                page_id="page123",
            )

        client_cls.assert_called_once_with("token", app_id=None)

    def test_publish_to_instagram_uses_local_file_with_app_id(self) -> None:
        job = PublishJob(
            title="Launch",
            description="Caption text",
            video_path="downloads/happyscribe/sample.mp4",
            video_format="short_form",
        )
        with (
            patch("media_publisher.publishers.instagram.MetaClient") as client_cls,
            patch(
                "media_publisher.publishers.instagram.ensure_instagram_upload_video",
                side_effect=self._passthrough_instagram_video,
            ),
        ):
            client_cls.return_value.schedule_instagram_reel.return_value = "ig_media_1"
            media_id = publish_to_instagram(
                job,
                instagram_account_id="ig123",
                access_token="token",
                app_id="app123",
            )

        self.assertEqual(media_id, "ig_media_1")
        client_cls.assert_called_once_with("token", app_id="app123")

    def test_schedule_facebook_reel_sets_thumbnail(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        with tempfile.TemporaryDirectory() as tmpdir:
            thumb = Path(tmpdir) / "cover.png"
            thumb.write_bytes(b"png")
            with (
                patch.object(client, "_request", return_value={"video_id": "vid_1", "upload_url": "https://upload.example"}) as request_mock,
                patch.object(client, "_upload_facebook_reel_video"),
                patch.object(client, "set_facebook_video_thumbnail") as thumb_mock,
            ):
                video_id = client.schedule_facebook_reel(
                    page_id="page123",
                    title="Launch",
                    description="Details",
                    video_path=Path("quote.mp4"),
                    thumbnail_path=thumb,
                )

        self.assertEqual(video_id, "vid_1")
        thumb_mock.assert_called_once_with("vid_1", thumb)
        self.assertEqual(request_mock.call_count, 2)

    def test_create_instagram_media_container_accepts_cover_url(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        with patch.object(client, "_request", return_value={"id": "ctr_1"}) as request_mock:
            container_id = client.create_instagram_media_container(
                instagram_account_id="ig123",
                caption="Caption",
                video_url="https://cdn.example.com/video.mp4",
                cover_url="https://cdn.example.com/cover.jpg",
            )

        self.assertEqual(container_id, "ctr_1")
        self.assertEqual(
            request_mock.call_args.kwargs["body"]["cover_url"],
            "https://cdn.example.com/cover.jpg",
        )

    def test_schedule_instagram_reel_hosts_local_cover(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        with tempfile.TemporaryDirectory() as tmpdir:
            thumb = Path(tmpdir) / "cover.png"
            thumb.write_bytes(b"png")
            with (
                patch.object(
                    client,
                    "upload_unpublished_photo",
                    return_value=FacebookHostingAsset(
                        asset_id="fb_photo_1",
                        url="https://cdn.example.com/cover.jpg",
                        kind="photo",
                    ),
                ) as upload_mock,
                patch.object(
                    client,
                    "create_instagram_resumable_reel_container",
                    return_value=("ctr_1", "https://rupload.facebook.com/ig-api-upload/v21.0/ctr_1"),
                ) as create_mock,
                patch.object(client, "upload_instagram_resumable_video"),
                patch.object(
                    client,
                    "wait_for_container",
                    return_value=type(
                        "Status",
                        (),
                        {"id": "ctr_1", "status_code": "FINISHED", "status": None},
                    )(),
                ),
                patch.object(client, "publish_instagram_container", return_value="ig_media_1"),
                patch.object(client, "cleanup_hosting_assets") as cleanup_mock,
            ):
                media_id = client.schedule_instagram_reel(
                    instagram_account_id="ig123",
                    caption="Caption",
                    video_path=Path("quote.mp4"),
                    page_id="page123",
                    cover_path=thumb,
                )

        self.assertEqual(media_id, "ig_media_1")
        upload_mock.assert_called_once_with("page123", thumb)
        self.assertEqual(
            create_mock.call_args.kwargs["cover_url"],
            "https://cdn.example.com/cover.jpg",
        )
        cleanup_mock.assert_called_once()
        cleanup_args = cleanup_mock.call_args.args
        self.assertEqual(len(cleanup_args), 1)
        self.assertEqual(cleanup_args[0].asset_id, "fb_photo_1")

    def test_schedule_instagram_image_cleans_up_hosted_photo(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        image_asset = FacebookHostingAsset(
            asset_id="fb_photo_1",
            url="https://cdn.example.com/quote.jpg",
            kind="photo",
        )
        with (
            patch.object(
                client,
                "upload_unpublished_photo",
                return_value=image_asset,
            ),
            patch.object(client, "create_instagram_image_container", return_value="ctr_1"),
            patch.object(
                client,
                "wait_for_container",
                return_value=type(
                    "Status",
                    (),
                    {"id": "ctr_1", "status_code": "FINISHED", "status": None},
                )(),
            ),
            patch.object(client, "publish_instagram_container", return_value="ig_media_1"),
            patch.object(client, "cleanup_hosting_assets") as cleanup_mock,
        ):
            media_id = client.schedule_instagram_image(
                instagram_account_id="ig123",
                caption="Caption",
                image_path=Path("quote.jpg"),
                page_id="page123",
            )

        self.assertEqual(media_id, "ig_media_1")
        cleanup_mock.assert_called_once_with(image_asset)

    def test_cleanup_hosting_assets_deletes_each_asset(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        assets = (
            FacebookHostingAsset(asset_id="vid_1", url="https://example.com/v.mp4", kind="video"),
            FacebookHostingAsset(asset_id="photo_1", url="https://example.com/p.jpg", kind="photo"),
        )
        with patch.object(client, "_request") as request_mock:
            client.cleanup_hosting_assets(*assets)

        self.assertEqual(request_mock.call_count, 2)
        request_mock.assert_any_call("DELETE", "vid_1")
        request_mock.assert_any_call("DELETE", "photo_1")

    def test_cleanup_hosting_assets_ignores_delete_failures(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        asset = FacebookHostingAsset(
            asset_id="vid_1",
            url="https://example.com/v.mp4",
            kind="video",
        )
        with patch.object(
            client,
            "_request",
            side_effect=MetaError("delete failed"),
        ) as request_mock:
            client.cleanup_hosting_assets(asset)

        request_mock.assert_called_once_with("DELETE", "vid_1")

    def test_publish_to_facebook_passes_thumbnail_for_short_form(self) -> None:
        job = PublishJob(
            title="Launch",
            video_url="https://cdn.example.com/video.mp4",
            video_format="short_form",
            thumbnail_path="downloads/canva/cover.png",
        )
        with patch("media_publisher.publishers.facebook.MetaClient") as client_cls:
            client_cls.return_value.schedule_facebook_reel.return_value = "fb_reel_1"
            publish_to_facebook(job, page_id="page123", access_token="token")

        reel_call = client_cls.return_value.schedule_facebook_reel.call_args
        self.assertEqual(reel_call.kwargs["thumbnail_path"], Path("downloads/canva/cover.png"))

    def test_publish_to_facebook_passes_thumbnail_for_post_format(self) -> None:
        job = PublishJob(
            title="Launch",
            video_url="https://cdn.example.com/video.mp4",
            video_format="post",
            thumbnail_path="downloads/canva/cover.png",
        )
        with patch("media_publisher.publishers.facebook.MetaClient") as client_cls:
            client_cls.return_value.schedule_facebook_video.return_value = "fb_video_1"
            publish_to_facebook(job, page_id="page123", access_token="token")

        video_call = client_cls.return_value.schedule_facebook_video.call_args
        self.assertEqual(video_call.kwargs["thumbnail_path"], Path("downloads/canva/cover.png"))

    def test_publish_to_instagram_passes_cover_for_short_form(self) -> None:
        job = PublishJob(
            title="Launch",
            description="Caption text",
            video_path="downloads/happyscribe/sample.mp4",
            video_format="short_form",
            thumbnail_path="downloads/canva/cover.png",
        )
        with (
            patch("media_publisher.publishers.instagram.MetaClient") as client_cls,
            patch(
                "media_publisher.publishers.instagram.ensure_instagram_upload_video",
                side_effect=self._passthrough_instagram_video,
            ),
        ):
            client_cls.return_value.schedule_instagram_reel.return_value = "ig_media_1"
            publish_to_instagram(
                job,
                instagram_account_id="ig123",
                access_token="token",
                app_id="app123",
                page_id="page123",
            )

        reel_call = client_cls.return_value.schedule_instagram_reel.call_args
        self.assertEqual(reel_call.kwargs["cover_path"], Path("downloads/canva/cover.png"))
        self.assertEqual(reel_call.kwargs["page_id"], "page123")


if __name__ == "__main__":
    unittest.main()
