"""Tests for YouTube community-post helpers that do not open a browser."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from media_publisher.publishers.youtube_web import (
    YouTubeCommunityLimitError,
    YouTubeWebError,
    community_post_permalink,
    extract_post_id,
    import_browser_session,
    interpret_create_post_response,
    storage_state_has_google_auth,
    timezone_match_needles,
    timezone_text_matches,
    youtube_posts_url,
)


class YouTubeWebHelperTests(unittest.TestCase):
    def test_posts_url_uses_the_channel_handle(self) -> None:
        self.assertEqual(
            youtube_posts_url("https://www.youtube.com/@SadhguruBulgarian"),
            "https://www.youtube.com/@SadhguruBulgarian/posts?hl=en&gl=US",
        )

    def test_permalink_prefers_the_post_id(self) -> None:
        self.assertEqual(
            community_post_permalink("UgkxABC", channel_handle="SadhguruBulgarian"),
            "https://www.youtube.com/post/UgkxABC",
        )
        self.assertIn(
            "/@SadhguruBulgarian/posts",
            community_post_permalink("  ", channel_handle="@SadhguruBulgarian"),
        )

    def test_extract_post_id_walks_nested_actions(self) -> None:
        payload = {
            "actions": [{"createBackstagePostAction": {"postId": "UgkxABC"}}]
        }
        self.assertEqual(extract_post_id(payload), "UgkxABC")
        self.assertEqual(interpret_create_post_response(200, payload), "UgkxABC")

    def test_create_post_limit_is_a_distinct_error(self) -> None:
        with self.assertRaises(YouTubeCommunityLimitError):
            interpret_create_post_response(
                400,
                {"error": {"message": "Post limit reached"}},
            )

    def test_create_post_other_errors_stay_generic(self) -> None:
        with self.assertRaises(YouTubeWebError):
            interpret_create_post_response(403, {"error": {"message": "forbidden"}})

    def test_sofia_offset_needles_follow_daylight_saving(self) -> None:
        winter = datetime(2026, 11, 8, 8, tzinfo=ZoneInfo("Europe/Sofia"))
        summer = datetime(2026, 7, 8, 8, tzinfo=ZoneInfo("Europe/Sofia"))
        winter_needles = timezone_match_needles(winter, "Europe/Sofia")
        summer_needles = timezone_match_needles(summer, "Europe/Sofia")
        self.assertIn("gmt+02:00", winter_needles)
        self.assertIn("gmt+2", winter_needles)
        self.assertIn("sofia", winter_needles)
        self.assertIn("gmt+03:00", summer_needles)
        self.assertTrue(
            timezone_text_matches("(GMT+02:00) Sofia", winter_needles)
        )
        self.assertTrue(timezone_text_matches("GMT+2", winter_needles))
        self.assertFalse(
            timezone_text_matches("(GMT-07:00) Los Angeles", winter_needles)
        )
        self.assertFalse(
            timezone_text_matches("Sadhguru Bulgarian", winter_needles)
        )
        self.assertFalse(
            timezone_text_matches("GMT+02:00", summer_needles)
        )

    def test_storage_state_requires_a_google_auth_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(
                json.dumps({"cookies": [{"name": "VISITOR", "value": "x"}]}),
                encoding="utf-8",
            )
            self.assertFalse(storage_state_has_google_auth(path))
            path.write_text(
                json.dumps(
                    {"cookies": [{"name": "SAPISID", "value": "secret", "domain": ".youtube.com"}]}
                ),
                encoding="utf-8",
            )
            self.assertTrue(storage_state_has_google_auth(path))

    def test_import_session_keeps_google_auth_cookies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "cookies.json"
            source.write_text(
                json.dumps(
                    [
                        {
                            "name": "SAPISID",
                            "value": "abc",
                            "domain": ".youtube.com",
                            "path": "/",
                            "secure": True,
                            "httpOnly": False,
                            "sameSite": "no_restriction",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            destination = root / "youtube-browser-state.json"
            import_browser_session(source, destination)
            saved = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(saved["cookies"][0]["sameSite"], "None")
            self.assertTrue(storage_state_has_google_auth(destination))

            empty = root / "empty.json"
            empty.write_text(
                json.dumps(
                    [{"name": "PREF", "value": "hl=en", "domain": ".youtube.com"}]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(YouTubeWebError):
                import_browser_session(empty, root / "rejected.json")


if __name__ == "__main__":
    unittest.main()
