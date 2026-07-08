from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from media_publisher.models import PlatformScheduleTask, PublishJob
from media_publisher.pipeline import (
    PublishPipelineSettings,
    group_tasks_by_record,
    run_publish_pipeline,
)
from media_publisher.sources.airtable import AirtableClient, AirtableRecord, FIELD_TITLE
from media_publisher.sources.happyscribe import (
    HappyScribeClient,
    HappyScribeLibraryLocation,
    HappyScribeTranscription,
    ensure_catalog_video_downloaded,
    find_transcription_for_catalog,
    normalize_name_for_catalog_match,
)
from media_publisher.sources.canva import CanvaError


def _task(record_id: str, platform: str, title: str = "Translated title") -> PlatformScheduleTask:
    publish_at = datetime(2026, 7, 4, 15, 0, tzinfo=timezone.utc)
    job = PublishJob(
        title=title,
        metadata={FIELD_TITLE: "Original catalog name"},
        airtable_record_id=record_id,
    )
    return PlatformScheduleTask(
        platform=platform,  # type: ignore[arg-type]
        publish_at=publish_at,
        job=job,
        record_id=record_id,
        record_fields={"Original Video Name": "Original catalog name"},
    )


class PipelineHelperTests(unittest.TestCase):
    def test_group_tasks_by_record(self) -> None:
        tasks = [
            _task("rec1", "instagram"),
            _task("rec1", "facebook"),
            _task("rec2", "youtube"),
        ]
        grouped = group_tasks_by_record(tasks)
        self.assertEqual(set(grouped), {"rec1", "rec2"})
        self.assertEqual([task.platform for task in grouped["rec1"]], ["facebook", "instagram"])

    def test_find_transcription_for_catalog_matches_stem(self) -> None:
        transcriptions = [
            HappyScribeTranscription(
                id="tx1",
                name="Launch video.mp4",
                state="automatic_done",
            )
        ]
        found = find_transcription_for_catalog(transcriptions, "Launch video")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, "tx1")

    def test_find_transcription_for_catalog_matches_srt_prefix_and_bg_suffix(self) -> None:
        transcriptions = [
            HappyScribeTranscription(
                id="tx1",
                name="SRT_Participation of Women in Economic Activity Will Ensure a Gentler Economy(bg)(1)",
                state="automatic_done",
            )
        ]
        found = find_transcription_for_catalog(
            transcriptions,
            "Participation of Women in Economic Activity Will Ensure a Gentler Economy",
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.id, "tx1")

    def test_normalize_name_for_catalog_match(self) -> None:
        self.assertEqual(
            normalize_name_for_catalog_match(
                "SRT_From Choosing Between Ducati(bg)"
            ),
            normalize_name_for_catalog_match(
                "From Choosing Between Ducati"
            ),
        )
        self.assertEqual(
            normalize_name_for_catalog_match(
                "Krishna Janmashtami Is Not Just About Krishnas Birth(bg).srt"
            ),
            normalize_name_for_catalog_match(
                "Krishna Janmashtami Is Not Just About Krishnas Birth"
            ),
        )

    def test_find_transcription_for_catalog_matches_srt_only_export(self) -> None:
        transcriptions = [
            HappyScribeTranscription(
                id="tx-srt",
                name="Launch video(bg).srt",
                state="automatic_done",
            )
        ]
        found = find_transcription_for_catalog(transcriptions, "Launch video")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, "tx-srt")

    def test_filter_tasks_for_local_date(self) -> None:
        from datetime import date

        from media_publisher.scheduling import filter_tasks_for_local_date

        tasks = [
            _task("rec1", "youtube"),
            _task("rec2", "facebook"),
        ]
        tasks[1] = PlatformScheduleTask(
            platform="facebook",
            publish_at=datetime(2026, 7, 5, 15, 0, tzinfo=timezone.utc),
            job=tasks[1].job,
            record_id="rec2",
            record_fields={},
        )
        filtered = filter_tasks_for_local_date(
            tasks,
            date(2026, 7, 4),
            publish_timezone="UTC",
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].record_id, "rec1")

    def test_find_transcription_for_catalog_skips_subtitled_exports(self) -> None:
        transcriptions = [
            HappyScribeTranscription(
                id="tx-sub",
                name="Launch video-subtitled.mp4",
                state="automatic_done",
            )
        ]
        self.assertIsNone(find_transcription_for_catalog(transcriptions, "Launch video"))

    def test_ensure_catalog_video_downloaded_uses_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            download_dir = Path(tmpdir)
            existing = download_dir / "Sample Title-subtitled.mp4"
            existing.write_bytes(b"video")
            client = HappyScribeClient("hs-test")
            location = HappyScribeLibraryLocation("1", "2")
            path = ensure_catalog_video_downloaded(
                "Sample Title",
                download_dir=download_dir,
                client=client,
                location=location,
                browser_state_path=download_dir / "session.json",
            )
        self.assertEqual(path, existing)

    def test_ensure_catalog_video_downloaded_force_regenerate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            download_dir = Path(tmpdir)
            existing = download_dir / "Sample Title-subtitled.mp4"
            existing.write_bytes(b"old")
            client = HappyScribeClient("hs-test")
            location = HappyScribeLibraryLocation("1", "2")
            transcription = HappyScribeTranscription(
                id="tx1",
                name="Sample Title",
                state="automatic_done",
            )
            def write_video(_id, destination, **_kwargs):
                destination.write_bytes(b"new")
                return destination

            with patch.object(
                client,
                "download_video_with_burned_subtitles",
                side_effect=write_video,
            ) as download_mock:
                path = ensure_catalog_video_downloaded(
                    "Sample Title",
                    download_dir=download_dir,
                    client=client,
                    location=location,
                    browser_state_path=download_dir / "missing-session.json",
                    transcriptions=[transcription],
                    force_regenerate=True,
                )
            download_mock.assert_called_once()
            self.assertEqual(path.name, "Sample Title-subtitled.mp4")
            self.assertEqual(path.read_bytes(), b"new")


class PublishPipelineTests(unittest.TestCase):
    def _pipeline_settings(self, **overrides) -> PublishPipelineSettings:
        values = {
            "project_root": Path("."),
            "publish_timezone": "Europe/Sofia",
            "publish_hour": 18,
            "canva_download_dir": Path("downloads/canva"),
            "canva_client": unittest.mock.Mock(),
            "canva_long_video_thumbnails_url": (
                "https://canva.link/mkc9c31v441jey0"
            ),
            "canva_short_video_thumbnails_url": (
                "https://canva.link/aqmh5jedqw5g0ei"
            ),
            "happyscribe_download_dir": Path("downloads/happyscribe"),
            "happyscribe_browser_state": Path("auth/happyscribe-session.json"),
            "happyscribe_browser_profile": None,
            "happyscribe_browser_channel": "chrome",
            "happyscribe_api_key": "hs-test",
            "happyscribe_headless": True,
            "ffmpeg_path": None,
            "youtube_client_secrets": Path("auth/youtube-client.json"),
            "youtube_token": Path("auth/youtube-token.json"),
            "youtube_channel_handle": "SadhguruBulgarian",
            "youtube_playlist_title": "Съзнателна Планета",
            "youtube_playlist_id": None,
            "template_urls": {},
            "meta_page_id": "page",
            "meta_instagram_account_id": "ig",
            "meta_access_token": "token",
            "meta_app_id": None,
        }
        values.update(overrides)
        return PublishPipelineSettings(**values)

    def test_run_publish_pipeline_no_tasks(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        happyscribe = HappyScribeClient("hs-test")
        location = HappyScribeLibraryLocation("1", "2")
        settings = self._pipeline_settings()
        with patch.object(client, "list_records", return_value=[]):
            exit_code, results = run_publish_pipeline(
                client,
                happyscribe,
                location,
                settings,
                print_line=lambda _: None,
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(results, [])

    def test_run_publish_pipeline_publishes_and_updates_airtable(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        happyscribe = HappyScribeClient("hs-test")
        location = HappyScribeLibraryLocation("1", "2")
        record = AirtableRecord(
            id="recABC",
            fields={
                "Status": "5. Synchronization done",
                FIELD_TITLE: "Launch video",
                "Video name translated": "Видео",
                "SG-YT-Date published": "2026-07-04",
            },
        )
        settings = self._pipeline_settings()
        video_path = Path("downloads/happyscribe/Launch video-subtitled.mp4")
        thumbnail_path = Path("downloads/canva/Launch video.png")

        with patch.object(client, "list_records", return_value=[record]), patch.object(
            happyscribe,
            "list_search_transcriptions",
            return_value=[],
        ), patch(
            "media_publisher.pipeline.ensure_catalog_video_downloaded",
            return_value=video_path,
        ), patch(
            "media_publisher.pipeline.ensure_catalog_thumbnail_from_canva",
            side_effect=lambda job, **_: replace(
                job,
                thumbnail_path=str(thumbnail_path),
            ),
        ), patch(
            "media_publisher.pipeline.publish_platform_task",
            return_value="https://www.youtube.com/watch?v=abc123",
        ) as publish_mock, patch.object(
            client,
            "update_record",
            return_value=AirtableRecord(id="recABC", fields={"SG-YT-Published video": "url"}),
        ) as update_mock:
            exit_code, results = run_publish_pipeline(
                client,
                happyscribe,
                location,
                settings,
                print_line=lambda _: None,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        publish_mock.assert_called_once()
        update_mock.assert_called_once()

    def test_run_publish_pipeline_continues_without_thumbnail_on_canva_error(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        happyscribe = HappyScribeClient("hs-test")
        location = HappyScribeLibraryLocation("1", "2")
        record = AirtableRecord(
            id="recABC",
            fields={
                "Status": "5. Synchronization done",
                FIELD_TITLE: "Launch video",
                "Video name translated": "Видео",
                "SG-YT-Date published": "2026-07-04",
            },
        )
        settings = self._pipeline_settings()
        video_path = Path("downloads/happyscribe/Launch video-subtitled.mp4")

        with patch.object(client, "list_records", return_value=[record]), patch.object(
            happyscribe,
            "list_search_transcriptions",
            return_value=[],
        ), patch(
            "media_publisher.pipeline.ensure_catalog_video_downloaded",
            return_value=video_path,
        ), patch(
            "media_publisher.pipeline.ensure_catalog_thumbnail_from_canva",
            side_effect=CanvaError("No thumbnail page matching title"),
        ), patch(
            "media_publisher.pipeline.publish_platform_task",
            return_value="https://www.youtube.com/watch?v=abc123",
        ) as publish_mock, patch.object(
            client,
            "update_record",
            return_value=AirtableRecord(id="recABC", fields={"SG-YT-Published video": "url"}),
        ):
            exit_code, results = run_publish_pipeline(
                client,
                happyscribe,
                location,
                settings,
                print_line=lambda _: None,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        publish_mock.assert_called_once()
        self.assertIsNone(publish_mock.call_args.args[0].job.thumbnail_path)

    def test_run_publish_pipeline_skips_instagram_when_private_test(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        happyscribe = HappyScribeClient("hs-test")
        location = HappyScribeLibraryLocation("1", "2")
        tasks = [
            _task("recABC", "youtube"),
            _task("recABC", "facebook"),
            _task("recABC", "instagram"),
        ]
        settings = self._pipeline_settings(
            publish_immediately=True,
            private_test=True,
        )
        video_path = Path("downloads/happyscribe/Launch video-subtitled.mp4")

        with patch(
            "media_publisher.pipeline.fetch_pending_schedule_tasks",
            return_value=tasks,
        ), patch.object(
            client,
            "list_records",
            return_value=[],
        ), patch.object(
            happyscribe,
            "list_search_transcriptions",
            return_value=[],
        ), patch(
            "media_publisher.pipeline.ensure_catalog_video_downloaded",
            return_value=video_path,
        ), patch(
            "media_publisher.pipeline.ensure_catalog_thumbnail_from_canva",
            side_effect=lambda job, **_: job,
        ), patch(
            "media_publisher.pipeline.publish_platform_task",
            return_value="https://example.com/post",
        ) as publish_mock, patch.object(
            client,
            "update_record",
            return_value=AirtableRecord(id="recABC", fields={}),
        ):
            exit_code, results = run_publish_pipeline(
                client,
                happyscribe,
                location,
                settings,
                meta_client=unittest.mock.Mock(),
                print_line=lambda _: None,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(results), 2)
        published_platforms = {result.platform for result in results}
        self.assertEqual(published_platforms, {"youtube", "facebook"})
        self.assertEqual(publish_mock.call_count, 2)
        self.assertTrue(
            all(call.args[0].platform != "instagram" for call in publish_mock.call_args_list)
        )

    def test_run_publish_pipeline_skips_instagram_when_video_too_long(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        happyscribe = HappyScribeClient("hs-test")
        location = HappyScribeLibraryLocation("1", "2")
        long_job = PublishJob(
            title="Translated title",
            metadata={FIELD_TITLE: "Original catalog name", "Duration": "1487"},
            airtable_record_id="recABC",
        )
        publish_at = datetime(2026, 7, 4, 15, 0, tzinfo=timezone.utc)
        tasks = [
            PlatformScheduleTask(
                platform="youtube",
                publish_at=publish_at,
                job=long_job,
                record_id="recABC",
            ),
            PlatformScheduleTask(
                platform="instagram",
                publish_at=publish_at,
                job=long_job,
                record_id="recABC",
            ),
        ]
        settings = self._pipeline_settings(publish_immediately=True)
        video_path = Path("downloads/happyscribe/Launch video-subtitled.mp4")
        messages: list[str] = []

        with patch(
            "media_publisher.pipeline.fetch_pending_schedule_tasks",
            return_value=tasks,
        ), patch.object(
            client,
            "list_records",
            return_value=[],
        ), patch.object(
            happyscribe,
            "list_search_transcriptions",
            return_value=[],
        ), patch(
            "media_publisher.pipeline.ensure_catalog_video_downloaded",
            return_value=video_path,
        ), patch(
            "media_publisher.pipeline.ensure_catalog_thumbnail_from_canva",
            side_effect=lambda job, **_: job,
        ), patch(
            "media_publisher.pipeline.publish_platform_task",
            return_value="https://example.com/post",
        ) as publish_mock, patch.object(
            client,
            "update_record",
            return_value=AirtableRecord(id="recABC", fields={}),
        ):
            exit_code, results = run_publish_pipeline(
                client,
                happyscribe,
                location,
                settings,
                meta_client=unittest.mock.Mock(),
                print_line=messages.append,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].platform, "youtube")
        self.assertEqual(publish_mock.call_count, 1)
        self.assertTrue(any("instagram: skipped" in message for message in messages))

    def test_run_publish_pipeline_skips_instagram_for_long_form_video(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        happyscribe = HappyScribeClient("hs-test")
        location = HappyScribeLibraryLocation("1", "2")
        long_form_job = PublishJob(
            title="Translated title",
            video_format="post",
            metadata={FIELD_TITLE: "Original catalog name"},
            airtable_record_id="recABC",
        )
        publish_at = datetime(2026, 7, 4, 15, 0, tzinfo=timezone.utc)
        tasks = [
            PlatformScheduleTask(
                platform="youtube",
                publish_at=publish_at,
                job=long_form_job,
                record_id="recABC",
            ),
            PlatformScheduleTask(
                platform="instagram",
                publish_at=publish_at,
                job=long_form_job,
                record_id="recABC",
            ),
        ]
        settings = self._pipeline_settings(publish_immediately=True)
        video_path = Path("downloads/happyscribe/Launch video-subtitled.mp4")
        messages: list[str] = []

        with patch(
            "media_publisher.pipeline.fetch_pending_schedule_tasks",
            return_value=tasks,
        ), patch.object(
            client,
            "list_records",
            return_value=[],
        ), patch.object(
            happyscribe,
            "list_search_transcriptions",
            return_value=[],
        ), patch(
            "media_publisher.pipeline.ensure_catalog_video_downloaded",
            return_value=video_path,
        ), patch(
            "media_publisher.pipeline.ensure_catalog_thumbnail_from_canva",
            side_effect=lambda job, **_: job,
        ), patch(
            "media_publisher.pipeline.publish_platform_task",
            return_value="https://example.com/post",
        ) as publish_mock, patch.object(
            client,
            "update_record",
            return_value=AirtableRecord(id="recABC", fields={}),
        ):
            exit_code, results = run_publish_pipeline(
                client,
                happyscribe,
                location,
                settings,
                meta_client=unittest.mock.Mock(),
                print_line=messages.append,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].platform, "youtube")
        self.assertEqual(publish_mock.call_count, 1)
        self.assertTrue(any("long-form Video uploads are not supported" in message for message in messages))


class PublishRunModeTests(unittest.TestCase):
    def test_resolve_publish_run_mode_defaults_to_immediate_today(self) -> None:
        from argparse import Namespace
        from datetime import date, datetime, timezone

        from media_publisher.__main__ import resolve_publish_run_mode

        args = Namespace(schedule=False, private=False)
        with (
            patch(
                "media_publisher.timezones.get_timezone",
                return_value=timezone.utc,
            ),
            patch("datetime.datetime", wraps=datetime) as datetime_cls,
        ):
            datetime_cls.now.return_value = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
            publish_immediately, publish_on_date, private_test = resolve_publish_run_mode(
                args,
                publish_timezone="UTC",
            )

        self.assertTrue(publish_immediately)
        self.assertEqual(publish_on_date, date(2026, 7, 5))
        self.assertFalse(private_test)

    def test_resolve_publish_run_mode_schedule_still_limits_to_today(self) -> None:
        from argparse import Namespace
        from datetime import date, datetime, timezone

        from media_publisher.__main__ import resolve_publish_run_mode

        args = Namespace(schedule=True, private=True)
        with (
            patch(
                "media_publisher.timezones.get_timezone",
                return_value=timezone.utc,
            ),
            patch("datetime.datetime", wraps=datetime) as datetime_cls,
        ):
            datetime_cls.now.return_value = datetime(2026, 7, 5, 18, 0, tzinfo=timezone.utc)
            publish_immediately, publish_on_date, private_test = resolve_publish_run_mode(
                args,
                publish_timezone="UTC",
            )

        self.assertFalse(publish_immediately)
        self.assertEqual(publish_on_date, date(2026, 7, 5))
        self.assertTrue(private_test)


class SelectedPlatformTests(unittest.TestCase):
    def test_resolve_selected_platforms_dedupes(self) -> None:
        from argparse import Namespace

        from media_publisher.__main__ import resolve_selected_platforms

        args = Namespace(platform=["youtube", "youtube", "facebook"])
        self.assertEqual(
            resolve_selected_platforms(args),
            ("youtube", "facebook"),
        )

    def test_resolve_selected_platforms_none_when_omitted(self) -> None:
        from argparse import Namespace

        from media_publisher.__main__ import resolve_selected_platforms

        self.assertIsNone(resolve_selected_platforms(Namespace(platform=None)))


if __name__ == "__main__":
    unittest.main()
