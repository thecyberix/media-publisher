from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from catalog_parser.smartcat import SmartcatError
from catalog_parser.smartcat_cookie import (
    cookies_from_import_payload,
    cookies_header,
    ensure_storage_state_file,
    load_storage_state_cookies,
    normalize_browser_cookie,
)


class SmartcatCookieTests(unittest.TestCase):
    def test_cookies_header_filters_by_host(self) -> None:
        cookies = [
            {"name": "sc", "value": "1", "domain": "ea.smartcat.com"},
            {"name": "other", "value": "2", "domain": "example.com"},
        ]
        header = cookies_header(cookies, host="ea.smartcat.com")
        self.assertEqual(header, "sc=1")

    def test_cookies_header_parent_domain(self) -> None:
        cookies = [{"name": "sc", "value": "abc", "domain": ".smartcat.com"}]
        header = cookies_header(cookies, host="ea.smartcat.com")
        self.assertEqual(header, "sc=abc")

    def test_load_storage_state_cookies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text(
                json.dumps({"cookies": [{"name": "a", "value": "b"}]}),
                encoding="utf-8",
            )
            cookies = load_storage_state_cookies(path)
            self.assertEqual(cookies[0]["name"], "a")

    def test_ensure_storage_state_file_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            destination = project_root / "smartcat-state.json"
            import os

            os.environ["SMARTCAT_STORAGE_STATE_JSON"] = json.dumps(
                {"cookies": [{"name": "session", "value": "xyz", "domain": "ea.smartcat.com"}]}
            )
            try:
                resolved = ensure_storage_state_file(destination, project_root=project_root)
            finally:
                os.environ.pop("SMARTCAT_STORAGE_STATE_JSON", None)

            self.assertEqual(resolved, destination)
            payload = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(payload["cookies"][0]["value"], "xyz")

    def test_normalize_cookie_editor_export(self) -> None:
        normalized = normalize_browser_cookie(
            {
                "domain": ".smartcat.com",
                "expirationDate": 1893456000.0,
                "hostOnly": False,
                "httpOnly": True,
                "name": "sc_session",
                "path": "/",
                "sameSite": "no_restriction",
                "secure": True,
                "session": False,
                "value": "abc123",
            }
        )
        assert normalized is not None
        self.assertEqual(normalized["name"], "sc_session")
        self.assertEqual(normalized["sameSite"], "None")
        self.assertEqual(normalized["expires"], 1893456000.0)

    def test_cookies_from_import_payload_accepts_array(self) -> None:
        cookies = cookies_from_import_payload(
            [{"name": "a", "value": "b", "domain": "ea.smartcat.com", "path": "/"}]
        )
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["name"], "a")

    def test_cookies_from_import_payload_rejects_hotcleaner_encrypted(self) -> None:
        with self.assertRaises(SmartcatError) as ctx:
            cookies_from_import_payload({"version": 2, "data": "abc123"})
        self.assertIn("password-encrypted", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
