from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_publisher.sources.canva_share_preview import (
    _screen_url,
    download_canva_share_preview,
)


SHARE_URL = (
    "https://www.canva.com/design/DAG_-usKEHQ/Gf7htm9P2120Plv54YiiNw/view"
    "?utm_content=DAG_-usKEHQ&utm_campaign=designshare"
    "&utm_medium=link&utm_source=publishsharelink&mode=preview"
)


class CanvaSharePreviewTests(unittest.TestCase):
    def test_screen_url_keeps_share_token(self) -> None:
        self.assertEqual(
            _screen_url(SHARE_URL),
            "https://www.canva.com/design/DAG_-usKEHQ/Gf7htm9P2120Plv54YiiNw/screen",
        )

    def test_screen_url_requires_share_token(self) -> None:
        with self.assertRaises(RuntimeError):
            _screen_url("https://www.canva.com/design/DAG_-usKEHQ/view")

    def test_download_uses_screen_url_without_dom_scrape(self) -> None:
        destination = Path(tempfile.mkdtemp()) / "preview.jpg"
        with patch(
            "media_publisher.sources.canva_share_preview.resolve_canva_url",
            return_value=SHARE_URL,
        ):
            with patch(
                "media_publisher.sources.canva_share_preview._fetch_dom",
            ) as fetch_dom:
                with patch(
                    "media_publisher.sources.canva_share_preview._download_url",
                    side_effect=lambda _url, dest: dest.write_bytes(b"preview" * 200),
                ) as download:
                    result = download_canva_share_preview(SHARE_URL, destination)

        self.assertEqual(result, destination)
        fetch_dom.assert_not_called()
        download.assert_called_once()
        self.assertEqual(
            download.call_args[0][0],
            "https://www.canva.com/design/DAG_-usKEHQ/Gf7htm9P2120Plv54YiiNw/screen",
        )
        self.assertGreaterEqual(destination.stat().st_size, 1000)
