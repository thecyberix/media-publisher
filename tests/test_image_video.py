from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_publisher.sources.image_video import (
    QUOTE_VIDEO_DURATION_SECONDS,
    ensure_quote_video,
)


class ImageVideoTests(unittest.TestCase):
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
