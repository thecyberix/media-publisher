from __future__ import annotations

import unittest
from pathlib import Path

from media_publisher.sources.source_thumbnail import (
    aspects_match,
    detect_platform,
    original_thumbnail_destination,
    parse_instagram_shortcode,
    parse_youtube_video_id,
    pick_matching_thumbnail_url,
    video_size_from_ytdlp_info,
    youtube_thumbnail_urls,
)


class SourceThumbnailHelperTests(unittest.TestCase):
    def test_parse_youtube_video_id(self) -> None:
        self.assertEqual(
            parse_youtube_video_id("https://youtu.be/4cFjryZyN78"),
            "4cFjryZyN78",
        )
        self.assertEqual(
            parse_youtube_video_id(
                "https://www.youtube.com/watch?v=0Cv93HrKTGc&feature=share"
            ),
            "0Cv93HrKTGc",
        )
        self.assertEqual(
            parse_youtube_video_id("https://www.youtube.com/shorts/abc123XYZ-_"),
            "abc123XYZ-_",
        )

    def test_parse_instagram_shortcode(self) -> None:
        self.assertEqual(
            parse_instagram_shortcode("https://www.instagram.com/p/DGm5VaMz_bF"),
            "DGm5VaMz_bF",
        )
        self.assertEqual(
            parse_instagram_shortcode("https://www.instagram.com/reel/DWnpG5qjkCh/"),
            "DWnpG5qjkCh",
        )

    def test_detect_platform(self) -> None:
        self.assertEqual(
            detect_platform("https://youtu.be/vSvyQ-8wJZw"),
            "youtube",
        )
        self.assertEqual(
            detect_platform("https://www.instagram.com/p/DIngk-EzKCX"),
            "instagram",
        )
        self.assertIsNone(detect_platform("https://example.com/video"))

    def test_youtube_thumbnail_urls(self) -> None:
        urls = youtube_thumbnail_urls("abc123XYZ-_")
        self.assertEqual(
            urls[0],
            "https://i.ytimg.com/vi/abc123XYZ-_/maxresdefault.jpg",
        )
        self.assertIn("hqdefault.jpg", urls[1])

    def test_original_thumbnail_destination(self) -> None:
        path = original_thumbnail_destination(
            Path("downloads/original-thumbnails"),
            'Title: "Demo"',
        )
        self.assertEqual(
            path,
            Path("downloads/original-thumbnails/Title_ _Demo_.original-thumb.jpg"),
        )

    def test_aspects_match(self) -> None:
        self.assertTrue(aspects_match(1920, 1080, 1280, 720))
        self.assertFalse(aspects_match(1080, 1920, 1280, 720))

    def test_video_size_from_ytdlp_info_prefers_best_format(self) -> None:
        info = {
            "formats": [
                {"vcodec": "none"},
                {"vcodec": "avc1", "width": 638, "height": 360},
                {"vcodec": "avc1", "width": 1276, "height": 720},
            ]
        }
        self.assertEqual(video_size_from_ytdlp_info(info), (1276, 720))

    def test_pick_matching_thumbnail_url_skips_portrait_for_landscape_video(self) -> None:
        info = {
            "thumbnail": "https://example.com/default.jpg",
            "thumbnails": [
                {"width": 1080, "height": 1920, "url": "https://example.com/portrait.jpg"},
                {"width": 1280, "height": 720, "url": "https://example.com/landscape.jpg"},
            ],
        }
        picked = pick_matching_thumbnail_url(info, (1276, 720))
        self.assertEqual(picked, "https://example.com/landscape.jpg")

    def test_pick_best_thumbnail_url_prefers_largest_image(self) -> None:
        info = {
            "thumbnail": "https://example.com/default.jpg",
            "thumbnails": [
                {"width": 640, "height": 640, "url": "https://example.com/small.jpg"},
                {"width": 1080, "height": 1920, "url": "https://example.com/large.jpg"},
            ],
        }
        from media_publisher.sources.source_thumbnail import pick_best_thumbnail_url

        picked = pick_best_thumbnail_url(info)
        self.assertEqual(picked, "https://example.com/large.jpg")


if __name__ == "__main__":
    unittest.main()
