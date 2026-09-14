from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from catalog_parser.canva import CanvaError
from catalog_parser.canva_web import (
    CanvaWebClient,
    _browser_channel_candidates,
    _is_canva_design_page,
    _is_usable_preview_url,
    _looks_like_cloudflare_challenge,
    _looks_like_login_url,
    download_canva_design_via_web,
    ensure_storage_state_file,
    load_storage_state_cookies,
)
from media_publisher.sources.canva import is_canva_auth_error
from media_publisher.sources.canva_share_preview import (
    pick_best_canva_media_url,
    preview_image_url_from_html,
)


class CanvaWebHelperTests(unittest.TestCase):
    def test_login_url_detection(self) -> None:
        self.assertTrue(_looks_like_login_url("https://www.canva.com/login?redirect=/design/x"))
        self.assertTrue(_looks_like_login_url("https://accounts.google.com/o/oauth2/auth"))
        self.assertFalse(_looks_like_login_url("https://www.canva.com/design/abc/view"))

    def test_cloudflare_challenge_detection(self) -> None:
        self.assertTrue(
            _looks_like_cloudflare_challenge(title="Just a moment...")
        )
        self.assertTrue(
            _looks_like_cloudflare_challenge(
                html='<script src="https://challenges.cloudflare.com/turnstile/v0/api.js">'
            )
        )
        self.assertFalse(
            _looks_like_cloudflare_challenge(
                title="If You Observe An Ant",
                html='<meta property="og:image" content="https://media.canva.com/x.jpg">',
            )
        )

    def test_design_page_and_preview_url_helpers(self) -> None:
        self.assertTrue(_is_canva_design_page("https://www.canva.com/design/abc/view"))
        self.assertFalse(_is_canva_design_page("https://www.canva.com/"))
        self.assertTrue(_is_usable_preview_url("https://media.canva.com/v2/image-resize/x.jpg"))
        self.assertFalse(_is_usable_preview_url("https://static.canva.com/static/images/og.jpg"))

    def test_accept_shared_design_clicks_invite_button(self) -> None:
        from catalog_parser.canva_web import _accept_shared_design_if_needed

        button = MagicMock()
        button.count.return_value = 1
        page = MagicMock()
        page.url = "https://www.canva.com/design/x/view"
        page.content.return_value = "You've been invited to edit this design"
        page.title.return_value = "Canva"
        page.get_by_role.return_value = button
        _accept_shared_design_if_needed(page)
        button.first.click.assert_called()
        from catalog_parser.canva_web import _looks_like_design_permission_denied

        self.assertTrue(
            _looks_like_design_permission_denied(
                html="You don't have permission to see this design"
            )
        )
        self.assertFalse(
            _looks_like_design_permission_denied(
                html='<meta property="og:image" content="https://media.canva.com/x.jpg">'
            )
        )

    def test_browser_channel_candidates(self) -> None:
        self.assertEqual(_browser_channel_candidates(None), ("chrome", "msedge"))
        self.assertEqual(_browser_channel_candidates("chrome")[0], "chrome")
        self.assertEqual(_browser_channel_candidates("msedge")[0], "msedge")
        self.assertEqual(_browser_channel_candidates("edge")[0], "msedge")

    def test_session_errors_are_not_oauth_auth_errors(self) -> None:
        self.assertFalse(
            is_canva_auth_error(
                CanvaError(
                    "Canva browser session not found. "
                    "Run: python -m catalog_parser --canva-login"
                )
            )
        )
        self.assertFalse(
            is_canva_auth_error(
                CanvaError("Canva session expired. Run: python -m catalog_parser --canva-login")
            )
        )

    def test_load_cookies_requires_file(self) -> None:
        missing = Path(tempfile.mkdtemp()) / "missing-canva-state.json"
        with self.assertRaises(CanvaError) as ctx:
            load_storage_state_cookies(missing)
        self.assertIn("--canva-login", str(ctx.exception))

    def test_ensure_storage_state_file_writes_env_blob(self) -> None:
        root = Path(tempfile.mkdtemp())
        destination = root / "canva-state.json"
        payload = json.dumps({"cookies": [{"name": "c", "value": "1", "domain": ".canva.com"}]})
        with patch.dict("os.environ", {"CANVA_STORAGE_STATE_JSON": payload}, clear=False):
            written = ensure_storage_state_file(destination, project_root=root)
        self.assertEqual(written, destination)
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["cookies"][0]["name"], "c")

    def test_preview_image_url_from_html_uses_og_image(self) -> None:
        html = '<meta property="og:image" content="https://media.canva.com/v2/image-resize/x.jpg">'
        self.assertEqual(
            preview_image_url_from_html(html),
            "https://media.canva.com/v2/image-resize/x.jpg",
        )

    def test_pick_best_canva_media_url_prefers_larger(self) -> None:
        urls = [
            "https://media.canva.com/v2/image-resize/width:100/height:50/a.jpg",
            "https://media.canva.com/v2/image-resize/width:800/height:450/b.jpg",
        ]
        self.assertIn("width:800", pick_best_canva_media_url(urls) or "")


class CanvaWebDownloadTests(unittest.TestCase):
    def test_download_requires_session(self) -> None:
        dest = Path(tempfile.mkdtemp()) / "out.jpg"
        with patch(
            "catalog_parser.canva_web.build_canva_web_client_from_env",
            return_value=None,
        ):
            with self.assertRaises(CanvaError) as ctx:
                download_canva_design_via_web(
                    "https://www.canva.com/design/abc/view",
                    dest,
                )
        self.assertIn("--canva-login", str(ctx.exception))

    def test_download_with_page_writes_image(self) -> None:
        client = CanvaWebClient(storage_state_path=Path("canva-state.json"))
        page = MagicMock()
        page.url = "https://www.canva.com/design/abc/view"
        page.content.return_value = (
            '<meta property="og:image" content="https://media.canva.com/v2/image-resize/ok.jpg">'
        )
        response = MagicMock()
        response.status = 200
        response.body.return_value = b"jpeg-bytes" * 20
        page.request.get.return_value = response

        dest = Path(tempfile.mkdtemp()) / "out.jpg"
        with patch(
            "media_publisher.sources.canva.resolve_canva_url",
            return_value="https://www.canva.com/design/abc/edit",
        ):
            client._download_with_page(
                page,
                "https://www.canva.com/design/abc/edit",
                dest,
            )

        self.assertEqual(dest.read_bytes(), b"jpeg-bytes" * 20)
        page.goto.assert_called_once()
        page.request.get.assert_called_once()

    def test_download_with_page_rejects_login_redirect(self) -> None:
        client = CanvaWebClient(storage_state_path=Path("canva-state.json"))
        page = MagicMock()
        page.url = "https://www.canva.com/login?redirect=/design/abc"
        page.content.return_value = "<html></html>"
        dest = Path(tempfile.mkdtemp()) / "out.jpg"
        with patch(
            "media_publisher.sources.canva.resolve_canva_url",
            return_value="https://www.canva.com/design/abc/view",
        ):
            with self.assertRaises(CanvaError) as ctx:
                client._download_with_page(
                    page,
                    "https://www.canva.com/design/abc/view",
                    dest,
                )
        self.assertIn("session expired", str(ctx.exception).casefold())

    def test_download_with_page_rejects_homepage_og_image(self) -> None:
        client = CanvaWebClient(storage_state_path=Path("canva-state.json"))
        page = MagicMock()
        page.url = "https://www.canva.com/"
        page.content.return_value = (
            '<meta property="og:image" content="https://static.canva.com/og.jpg">'
        )
        dest = Path(tempfile.mkdtemp()) / "out.jpg"
        with patch(
            "media_publisher.sources.canva.resolve_canva_url",
            return_value="https://www.canva.com/design/DAG_-usKEHQ",
        ):
            with self.assertRaises(CanvaError) as ctx:
                client._download_with_page(
                    page,
                    "https://www.canva.com/design/DAG_-usKEHQ",
                    dest,
                )
        self.assertIn("ended on", str(ctx.exception))
        self.assertGreaterEqual(page.goto.call_count, 2)


if __name__ == "__main__":
    unittest.main()
