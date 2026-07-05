from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_publisher.sources.image_video import (
    ImageVideoError,
    ensure_short_with_cover_at_end,
)


class ImageVideoTests(unittest.TestCase):
    def test_ensure_short_with_cover_at_end_requires_ffmpeg(self) -> None:
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
                    ensure_short_with_cover_at_end(video, thumb)

    def test_ensure_short_with_cover_at_end_uses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video = root / "clip.mp4"
            thumb = root / "cover.png"
            video.write_bytes(b"video")
            thumb.write_bytes(b"png")
            baked = root / "clip.youtube-short-cover-end.mp4"
            baked.write_bytes(b"baked")

            with patch(
                "media_publisher.sources.image_video._run_ffmpeg"
            ) as ffmpeg_mock:
                result = ensure_short_with_cover_at_end(
                    video,
                    thumb,
                    ffmpeg_path="ffmpeg",
                )

        self.assertEqual(result.resolve(), baked.resolve())
        ffmpeg_mock.assert_not_called()

    def test_ensure_short_with_cover_at_end_appends_static_cover(self) -> None:
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
                "media_publisher.sources.image_video._resolve_ffmpeg",
                return_value="ffmpeg",
            ):
                with patch(
                    "media_publisher.sources.image_video._run_ffmpeg",
                    side_effect=fake_ffmpeg,
                ) as ffmpeg_mock:
                    result = ensure_short_with_cover_at_end(
                        video,
                        thumb,
                        ffmpeg_path="ffmpeg",
                        outro_seconds=2.0,
                    )

        self.assertEqual(
            result.resolve(),
            (root / "clip.youtube-short-cover-end.mp4").resolve(),
        )
        ffmpeg_mock.assert_called_once()
        command = ffmpeg_mock.call_args.args[0]
        self.assertEqual(command[0], "ffmpeg")
        self.assertEqual(Path(command[command.index("-i") + 1]).resolve(), video.resolve())
        self.assertEqual(command[command.index("-t") + 1], "2.0")
        second_input_index = command.index("-i", command.index("-i") + 1)
        thumb_arg = command[second_input_index + 1]
        self.assertEqual(Path(thumb_arg).resolve(), thumb.resolve())
        filter_arg = command[command.index("-filter_complex") + 1]
        self.assertIn("concat=n=2:v=1:a=0", filter_arg)


if __name__ == "__main__":
    unittest.main()
