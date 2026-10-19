from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from media_publisher.sources.canva_share_preview import (
    _preview_looks_valid,
    _screen_url,
    download_canva_share_preview,
)


SHARE_URL = (
    "https://www.canva.com/design/DAG_-usKEHQ/Gf7htm9P2120Plv54YiiNw/view"
    "?utm_content=DAG_-usKEHQ&utm_campaign=designshare"
    "&utm_medium=link&utm_source=publishsharelink&mode=preview"
)


def _write_test_jpeg(path: Path) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (64, 64))
    image.putdata(
        [(index % 256, (index * 3) % 256, (index * 7) % 256) for index in range(64 * 64)]
    )
    image.save(path, "JPEG", quality=90)
    return path


class CanvaSharePreviewTests(unittest.TestCase):
    def test_screen_url_keeps_share_token(self) -> None:
        self.assertEqual(
            _screen_url(SHARE_URL),
            "https://www.canva.com/design/DAG_-usKEHQ/Gf7htm9P2120Plv54YiiNw/screen",
        )

    def test_screen_url_requires_share_token(self) -> None:
        with self.assertRaises(RuntimeError):
            _screen_url("https://www.canva.com/design/DAG_-usKEHQ/view")

    def test_preview_looks_valid_rejects_html(self) -> None:
        destination = Path(tempfile.mkdtemp()) / "preview.jpg"
        destination.write_text("<html>" + ("login wall " * 200) + "</html>", encoding="utf-8")
        self.assertFalse(_preview_looks_valid(destination))

    def test_download_uses_screen_url_without_dom_scrape(self) -> None:
        destination = Path(tempfile.mkdtemp()) / "preview.jpg"

        def _fake_download(_url: str, dest: Path, timeout: int = 120) -> None:
            del timeout
            _write_test_jpeg(dest)

        with patch(
            "media_publisher.sources.canva_share_preview.resolve_canva_url",
            return_value=SHARE_URL,
        ):
            with patch(
                "media_publisher.sources.canva_share_preview._fetch_page_html",
            ) as fetch_html:
                with patch(
                    "media_publisher.sources.canva_share_preview._download_url",
                    side_effect=_fake_download,
                ) as download:
                    result = download_canva_share_preview(SHARE_URL, destination)

        self.assertEqual(result, destination)
        fetch_html.assert_not_called()
        download.assert_called_once()
        self.assertEqual(
            download.call_args[0][0],
            "https://www.canva.com/design/DAG_-usKEHQ/Gf7htm9P2120Plv54YiiNw/screen",
        )
        self.assertTrue(_preview_looks_valid(destination))

    def test_download_uses_embedded_document_image_when_screen_is_blocked(self) -> None:
        destination = Path(tempfile.mkdtemp()) / "preview.jpg"
        document_url = (
            "https://media.canva.com/v2/document-image/hash:1/height:596/"
            "id:DAG_-usKEHQ/type:B/width:335?page=1"
        )
        cover_url = (
            "https://media.canva.com/v2/image-resize/format:PNG/height:1920/"
            "quality:100/width:1080"
        )
        html = (
            '"page":1,"height":596,"width":335,"url":"' + document_url + '" '
            + cover_url
        )
        downloaded: list[str] = []

        def _download(url: str, dest: Path, timeout: int = 120) -> None:
            del timeout
            downloaded.append(url)
            if url.endswith("/screen"):
                raise URLError("Expected image Content-Type")
            self.assertEqual(url, document_url)
            _write_test_jpeg(dest)

        with patch(
            "media_publisher.sources.canva_share_preview.resolve_canva_url",
            return_value=SHARE_URL,
        ):
            with patch(
                "media_publisher.sources.canva_share_preview._fetch_page_html",
                return_value=html,
            ):
                with patch(
                    "media_publisher.sources.canva_share_preview._download_url",
                    side_effect=_download,
                ):
                    with patch(
                        "media_publisher.sources.canva_share_preview._download_screen_with_browser",
                    ) as browser:
                        result = download_canva_share_preview(SHARE_URL, destination)

        self.assertEqual(result, destination)
        browser.assert_not_called()
        self.assertEqual(downloaded, [
            "https://www.canva.com/design/DAG_-usKEHQ/Gf7htm9P2120Plv54YiiNw/screen",
            document_url,
        ])
        self.assertNotIn(cover_url, downloaded)
        self.assertTrue(_preview_looks_valid(destination))

    def test_download_opens_screen_in_chromium_when_no_document_image(self) -> None:
        destination = Path(tempfile.mkdtemp()) / "preview.jpg"

        def _http_fails(url: str, dest: Path, timeout: int = 120) -> None:
            del dest, timeout
            self.assertTrue(url.endswith("/screen"))
            raise URLError("Expected image Content-Type")

        def _chromium(url: str, dest: Path) -> bool:
            self.assertTrue(url.endswith("/screen"))
            _write_test_jpeg(dest)
            return True

        with patch(
            "media_publisher.sources.canva_share_preview.resolve_canva_url",
            return_value=SHARE_URL,
        ):
            with patch(
                "media_publisher.sources.canva_share_preview._fetch_page_html",
                return_value="<html>no composed image</html>",
            ):
                with patch(
                    "media_publisher.sources.canva_share_preview._download_url",
                    side_effect=_http_fails,
                ):
                    with patch(
                        "media_publisher.sources.canva_share_preview._download_screen_with_browser",
                        side_effect=_chromium,
                    ):
                        result = download_canva_share_preview(SHARE_URL, destination)

        self.assertEqual(result, destination)
        self.assertTrue(_preview_looks_valid(destination))

    def test_download_raises_when_screen_and_document_image_are_missing(self) -> None:
        destination = Path(tempfile.mkdtemp()) / "preview.jpg"

        def _http_fails(url: str, dest: Path, timeout: int = 120) -> None:
            del url, dest, timeout
            raise URLError("Expected image Content-Type")

        with patch(
            "media_publisher.sources.canva_share_preview.resolve_canva_url",
            return_value=SHARE_URL,
        ):
            with patch(
                "media_publisher.sources.canva_share_preview._fetch_page_html",
                return_value="<html>login wall</html>",
            ):
                with patch(
                    "media_publisher.sources.canva_share_preview._download_url",
                    side_effect=_http_fails,
                ):
                    with patch(
                        "media_publisher.sources.canva_share_preview._download_screen_with_browser",
                        return_value=False,
                    ):
                        with self.assertRaises(RuntimeError) as ctx:
                            download_canva_share_preview(SHARE_URL, destination)

        self.assertIn("/screen image was not available", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
