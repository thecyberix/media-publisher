from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from media_publisher.models import PublishJob
from media_publisher.publishers.facebook import FacebookPublishError, publish_to_facebook
from media_publisher.publishers.instagram import InstagramPublishError, publish_to_instagram
from media_publisher.publishers.meta import (
    MetaClient,
    MetaError,
    normalize_facebook_page_username,
    normalize_instagram_username,
    validate_publish_at,
)
from media_publisher.sources.airtable import AirtableRecord, record_to_publish_job


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
    def test_record_to_publish_job_maps_publish_at(self) -> None:
        job = record_to_publish_job(
            AirtableRecord(
                id="recABC",
                fields={
                    "Original Video Name": "Sample Title",
                    "Publish At": "2026-07-05T10:00:00.000Z",
                },
            )
        )
        self.assertIsNotNone(job.publish_at)
        assert job.publish_at is not None
        self.assertEqual(job.publish_at.year, 2026)
        self.assertEqual(job.publish_at.month, 7)
        self.assertEqual(job.publish_at.day, 5)


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

    def test_schedule_facebook_video_with_url(self) -> None:
        client = MetaClient("token-test", app_id="app123")
        publish_at = datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc)
        with patch.object(client, "_multipart_request") as request_mock:
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


class PublisherWrapperTests(unittest.TestCase):
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
            post_id = publish_to_facebook(job, page_id="page123", access_token="token")

        self.assertEqual(post_id, "fb_video_1")
        client_cls.return_value.schedule_facebook_video.assert_called_once()

    def test_publish_to_instagram_uses_local_file_with_app_id(self) -> None:
        job = PublishJob(
            title="Launch",
            description="Caption text",
            video_path="downloads/happyscribe/sample.mp4",
        )
        with patch("media_publisher.publishers.instagram.MetaClient") as client_cls:
            client_cls.return_value.schedule_instagram_reel.return_value = "ig_media_1"
            media_id = publish_to_instagram(
                job,
                instagram_account_id="ig123",
                access_token="token",
                app_id="app123",
            )

        self.assertEqual(media_id, "ig_media_1")
        client_cls.assert_called_once_with("token", app_id="app123")


if __name__ == "__main__":
    unittest.main()
