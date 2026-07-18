import unittest
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from media_publisher.video_duration import (
    MAX_INSTAGRAM_VIDEO_SECONDS,
    instagram_duration_skip_message,
    instagram_exceeds_api_limit,
    parse_duration_seconds,
    resolve_video_duration_seconds,
)


class ParseDurationTests(unittest.TestCase):
    def test_parse_numeric_seconds(self) -> None:
        self.assertEqual(parse_duration_seconds(1487), 1487.0)
        self.assertEqual(parse_duration_seconds("900"), 900.0)

    def test_parse_hms(self) -> None:
        self.assertEqual(parse_duration_seconds("24:47"), 24 * 60 + 47)
        self.assertEqual(parse_duration_seconds("1:05:30"), 3930.0)


class InstagramDurationLimitTests(unittest.TestCase):
    def test_exactly_fifteen_minutes_is_allowed(self) -> None:
        self.assertFalse(instagram_exceeds_api_limit(MAX_INSTAGRAM_VIDEO_SECONDS))

    def test_over_fifteen_minutes_is_skipped(self) -> None:
        self.assertTrue(instagram_exceeds_api_limit(MAX_INSTAGRAM_VIDEO_SECONDS + 1))

    def test_unknown_duration_is_not_skipped(self) -> None:
        self.assertFalse(instagram_exceeds_api_limit(None))

    def test_skip_message(self) -> None:
        self.assertIn("24.8 minutes", instagram_duration_skip_message(1487.0))

    def test_long_form_video_is_skipped_by_format(self) -> None:
        from media_publisher.publishers.instagram import (
            INSTAGRAM_VIDEO_TYPE_SKIP_MESSAGE,
            InstagramPublishError,
            publish_to_instagram,
        )
        from media_publisher.models import PublishJob

        job = PublishJob(
            title="Launch",
            description="Caption",
            video_path="downloads/happyscribe/sample.mp4",
            video_format="post",
        )
        with self.assertRaises(InstagramPublishError) as raised:
            publish_to_instagram(
                job,
                instagram_account_id="ig123",
                access_token="token",
                app_id="app123",
            )
        self.assertEqual(str(raised.exception), INSTAGRAM_VIDEO_TYPE_SKIP_MESSAGE)

    def test_resolve_from_metadata(self) -> None:
        duration = resolve_video_duration_seconds(
            metadata={"Duration": "1487"},
        )
        self.assertEqual(duration, 1487.0)


class InstagramUploadVideoPrepTests(unittest.TestCase):
    def test_instagram_upload_cache_path(self) -> None:
        from media_publisher.video_duration import instagram_upload_cache_path

        source = Path("downloads/happyscribe/video-subtitled.mp4")
        self.assertEqual(
            instagram_upload_cache_path(source),
            Path("downloads/happyscribe/video-subtitled-ig-upload.mp4"),
        )

    def test_ensure_instagram_upload_video_runs_ffmpeg(self) -> None:
        from media_publisher.video_duration import ensure_instagram_upload_video

        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "clip.mp4"
            source.write_bytes(b"source")
            destination = source.with_name("clip-ig-upload.mp4")

            def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                self.assertEqual(command[0], "ffmpeg")
                self.assertIn("+faststart", command)
                destination.write_bytes(b"prepared")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch(
                    "media_publisher.video_duration._resolve_ffmpeg",
                    return_value="ffmpeg",
                ),
                patch("media_publisher.video_duration.subprocess.run", side_effect=fake_run),
            ):
                prepared = ensure_instagram_upload_video(source)

            self.assertEqual(prepared.resolve(), destination.resolve())
            self.assertTrue(destination.is_file())


if __name__ == "__main__":
    unittest.main()
