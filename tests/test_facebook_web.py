"""Unit tests for temporary Facebook Playwright photo helpers."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_publisher.publishers.facebook_web import (
    business_suite_composer_url,
    extract_facebook_post_permalink,
    facebook_photo_via_browser_enabled,
    import_browser_session,
    page_feed_url,
    resolve_facebook_browser_channel,
    storage_state_has_auth_cookies,
)


class FacebookWebHelpersTest(unittest.TestCase):
    def test_browser_context_accepts_timezone_id(self) -> None:
        """Schedule spinbuttons use the browser clock; CI runners are UTC.

        Playwright must launch with timezone_id=Europe/Sofia so 8:00 AM in the
        UI is Sofia time (not 8:00 UTC → 11:00 Sofia).
        """
        import inspect

        from media_publisher.publishers.facebook_web import (
            _launch_storage_context,
            _facebook_page,
        )

        self.assertIn("timezone_id", inspect.signature(_launch_storage_context).parameters)
        self.assertIn("timezone_id", inspect.signature(_facebook_page).parameters)
    def test_browser_enabled_by_session_secret(self) -> None:
        with patch.dict(
            os.environ,
            {"FACEBOOK_BROWSER_STATE_JSON": '{"cookies":[]}'},
            clear=False,
        ):
            self.assertTrue(facebook_photo_via_browser_enabled())

    def test_browser_enabled_by_auth_storage_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "credentials" / "facebook-browser-state.json"
            state.parent.mkdir(parents=True)
            state.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {
                                "name": "c_user",
                                "value": "1",
                                "domain": ".facebook.com",
                                "path": "/",
                            },
                            {
                                "name": "xs",
                                "value": "1",
                                "domain": ".facebook.com",
                                "path": "/",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"FACEBOOK_BROWSER_STATE_JSON": ""}, clear=False):
                self.assertTrue(facebook_photo_via_browser_enabled(project_root=root))

    def test_browser_disabled_without_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"FACEBOOK_BROWSER_STATE_JSON": ""}, clear=False):
                self.assertFalse(facebook_photo_via_browser_enabled(project_root=root))

    def test_resolve_channel_ci_defaults_to_chromium(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "FACEBOOK_BROWSER_CHANNEL"}
        env["GITHUB_ACTIONS"] = "true"
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(resolve_facebook_browser_channel())
        with patch.dict(
            os.environ,
            {"GITHUB_ACTIONS": "true", "FACEBOOK_BROWSER_CHANNEL": "chrome"},
            clear=False,
        ):
            self.assertEqual(resolve_facebook_browser_channel(), "chrome")
        with patch.dict(
            os.environ, {"FACEBOOK_BROWSER_CHANNEL": "chromium"}, clear=False
        ):
            self.assertIsNone(resolve_facebook_browser_channel())

    def test_page_feed_url(self) -> None:
        self.assertEqual(
            page_feed_url("@SadhguruBulgarian"),
            "https://www.facebook.com/SadhguruBulgarian",
        )

    def test_extract_permalink_prefers_page_posts(self) -> None:
        html = (
            'noise https://www.facebook.com/other/posts/1 '
            'https://www.facebook.com/SadhguruBulgarian/posts/1234567890 more'
        )
        self.assertEqual(
            extract_facebook_post_permalink(html, page_username="SadhguruBulgarian"),
            "https://www.facebook.com/SadhguruBulgarian/posts/1234567890",
        )

    def test_business_suite_composer_url(self) -> None:
        url = business_suite_composer_url(
            asset_id="108518418983337",
            business_id="2391168584397645",
        )
        self.assertIn("business.facebook.com/latest/composer/", url)
        self.assertIn("asset_id=108518418983337", url)
        self.assertIn("business_id=2391168584397645", url)

    def test_storage_state_has_auth_cookies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.assertFalse(storage_state_has_auth_cookies(path))
            path.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {"name": "datr", "value": "x", "domain": ".facebook.com", "path": "/"},
                            {"name": "c_user", "value": "1", "domain": ".facebook.com", "path": "/"},
                            {"name": "xs", "value": "y", "domain": ".facebook.com", "path": "/"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(storage_state_has_auth_cookies(path))

    def test_cookie_editor_import(self) -> None:
        raw = [
            {
                "domain": ".facebook.com",
                "expirationDate": 1824876237.8,
                "httpOnly": True,
                "name": "c_user",
                "path": "/",
                "sameSite": "no_restriction",
                "secure": True,
                "value": "123",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "cookies.json"
            dst = Path(tmp) / "state.json"
            src.write_text(json.dumps(raw), encoding="utf-8")
            import_browser_session(src, dst)
            payload = json.loads(dst.read_text(encoding="utf-8"))
            self.assertEqual(payload["cookies"][0]["name"], "c_user")
            self.assertEqual(payload["cookies"][0]["sameSite"], "None")
            self.assertEqual(payload["origins"], [])


if __name__ == "__main__":
    unittest.main()
