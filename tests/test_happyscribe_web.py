from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from media_publisher.sources.happyscribe_web import (
    _browser_channel_candidates,
    _cookies_header,
    import_browser_session,
)


class HappyScribeWebTests(unittest.TestCase):
    def test_cookies_header(self) -> None:
        header = _cookies_header(
            [
                {"name": "session", "value": "abc123"},
                {"name": "token", "value": "xyz"},
            ]
        )
        self.assertEqual(header, "session=abc123; token=xyz")

    def test_browser_channel_candidates(self) -> None:
        self.assertEqual(_browser_channel_candidates("msedge")[0], "msedge")

    def test_import_browser_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.json"
            destination = Path(tmpdir) / "dest.json"
            source.write_text(
                json.dumps({"cookies": [{"name": "a", "value": "b"}]}),
                encoding="utf-8",
            )
            import_browser_session(source, destination)
            payload = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(payload["cookies"][0]["name"], "a")


if __name__ == "__main__":
    unittest.main()
