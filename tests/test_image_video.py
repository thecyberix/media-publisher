from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_publisher.sources.image_video import (
    ImageVideoError,
    QUOTE_VIDEO_DURATION_SECONDS,
    SHORT_COVER_INTRO_SECONDS,
    ensure_quote_video,
    ensure_short_with_cover_intro,
)


class ImageVideoTests(unittest.TestCase):
    def test_ensure_short_with_cover_intro_requires_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video = root / "clip.mp4"
            thumb = root / "cover.png"
            video.write_bytes(b"video")
            thumb.write_bytes(b"png")
            with patch(
                "media_publisher.sources.image_video._resolve_ffmpeg",
                side_effect=ImageVideoError("missing ffmpeg"),
            ):
                with self.assertRaises(ImageVideoError):
                    ensure_short_with_cover_intro(video, thumb)

    def test_ensure_short_with_cover_intro_uses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video = root / "clip.mp4"
            thumb = root / "cover.png"
            video.write_bytes(b"video")
            thumb.write_bytes(b"png")
            baked = root / "clip.youtube-short-cover-intro.mp4"
            baked.write_bytes(b"baked")

            with patch(
                "media_publisher.sources.image_video._run_ffmpeg"
            ) as ffmpeg_mock:
                result = ensure_short_with_cover_intro(
                    video,
                    thumb,
                    ffmpeg_path="ffmpeg",
                )

        self.assertEqual(result.resolve(), baked.resolve())
        ffmpeg_mock.assert_not_called()

    def test_ensure_short_with_cover_intro_prepends_static_cover(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video = root / "clip.mp4"
            thumb = root / "cover.png"
            video.write_bytes(b"video")
            thumb.write_bytes(b"png")

            def fake_ffmpeg(command: list[str], *, action: str) -> None:
                output = Path(command[-1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"mp4")

            with patch(
                "media_publisher.sources.image_video._video_has_audio_stream",
                return_value=True,
            ):
                with patch(
                    "media_publisher.sources.image_video._resolve_ffmpeg",
                    return_value="ffmpeg",
                ):
                    with patch(
                        "media_publisher.sources.image_video._run_ffmpeg",
                        side_effect=fake_ffmpeg,
                    ) as ffmpeg_mock:
                        result = ensure_short_with_cover_intro(
                            video,
                            thumb,
                            ffmpeg_path="ffmpeg",
                            intro_seconds=5.0,
                        )

        self.assertEqual(
            result.resolve(),
            (root / "clip.youtube-short-cover-intro.mp4").resolve(),
        )
        ffmpeg_mock.assert_called_once()
        command = ffmpeg_mock.call_args.args[0]
        self.assertEqual(command[0], "ffmpeg")
        thumb_arg = command[command.index("-i") + 1]
        video_arg = command[command.index("-i", command.index("-i") + 1) + 1]
        self.assertEqual(Path(thumb_arg).resolve(), thumb.resolve())
        self.assertEqual(Path(video_arg).resolve(), video.resolve())
        self.assertEqual(command[command.index("-t") + 1], "5.0")
        filter_arg = command[command.index("-filter_complex") + 1]
        self.assertIn("[intro][main]concat=n=2:v=1:a=0", filter_arg)
        self.assertIn("adelay=5000|5000", filter_arg)
        self.assertIn("[aout]", command)

    def test_default_intro_duration_is_five_seconds(self) -> None:
        self.assertEqual(SHORT_COVER_INTRO_SECONDS, 5.0)

    def test_ensure_quote_video_uses_ten_second_cache_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            image = root / "2026-07-05.png"
            image.write_bytes(b"png")
            cached = root / "videos" / "2026-07-05_quote_10s.mp4"
            cached.parent.mkdir()
            cached.write_bytes(b"mp4")

            with patch(
                "media_publisher.sources.image_video._run_ffmpeg"
            ) as ffmpeg_mock:
                result = ensure_quote_video(image, root / "videos", ffmpeg_path="ffmpeg")

        self.assertEqual(result.resolve(), cached.resolve())
        ffmpeg_mock.assert_not_called()
        self.assertEqual(QUOTE_VIDEO_DURATION_SECONDS, 10.0)


if __name__ == "__main__":
    unittest.main()
