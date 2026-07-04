from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_publisher.models import PublishJob
from media_publisher.sources.canva import (
    METADATA_CANVA_DESIGN_ID,
    CanvaClient,
    CanvaError,
    CanvaPendingAuth,
    CanvaToken,
    build_authorization_url,
    design_id_from_job,
    enrich_job_from_canva,
    generate_code_challenge,
    is_shortlink,
    load_pending_auth,
    load_token,
    parse_design_id,
    save_pending_auth,
    save_token,
    token_from_response,
)


class CanvaHelperTests(unittest.TestCase):
    def test_parse_design_id_from_url(self) -> None:
        design_id = parse_design_id("https://www.canva.com/design/DAGabc123/view")
        self.assertEqual(design_id, "DAGabc123")

    def test_parse_design_id_from_raw_value(self) -> None:
        self.assertEqual(parse_design_id("DAVZr1z5464"), "DAVZr1z5464")

    def test_is_shortlink(self) -> None:
        self.assertTrue(is_shortlink("https://canva.link/m05v8q5loz5oe11"))
        self.assertFalse(is_shortlink("https://www.canva.com/design/DAGabc123/view"))

    def test_parse_design_id_from_shortlink(self) -> None:
        with patch("media_publisher.sources.canva.resolve_canva_url") as resolve_mock:
            resolve_mock.return_value = (
                "https://www.canva.com/design/DAFn3LBegbg/lLiZrk2bugRboLob37nZrQ/edit"
            )
            design_id = parse_design_id("https://canva.link/m05v8q5loz5oe11")
        self.assertEqual(design_id, "DAFn3LBegbg")
        resolve_mock.assert_called_once()

    def test_download_design_images_downloads_all_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            client = CanvaClientTests()._client(tmpdir)
            download_dir = Path(tmpdir) / "out"
            with patch("media_publisher.sources.canva.resolve_canva_url") as resolve_mock, patch.object(
                client, "export_design"
            ) as export_mock, patch.object(
                client, "download_file", side_effect=lambda url, dest: dest
            ) as download_mock:
                resolve_mock.return_value = (
                    "https://www.canva.com/design/DAFn3LBegbg/lLiZrk2bugRboLob37nZrQ/edit"
                )
                export_mock.return_value = type(
                    "Job",
                    (),
                    {
                        "urls": (
                            "https://export-download.canva.com/page1.png",
                            "https://export-download.canva.com/page2.png",
                        )
                    },
                )()
                paths = client.download_design_images(
                    "https://canva.link/m05v8q5loz5oe11",
                    download_dir,
                )
            self.assertEqual(len(paths), 2)
            self.assertEqual(paths[0].name, "DAFn3LBegbg_page1.png")
            self.assertEqual(paths[1].name, "DAFn3LBegbg_page2.png")
            self.assertEqual(download_mock.call_count, 2)

    def test_generate_code_challenge_is_url_safe(self) -> None:
        verifier = "test-verifier-value"
        challenge = generate_code_challenge(verifier)
        self.assertNotIn("+", challenge)
        self.assertNotIn("/", challenge)
        self.assertNotIn("=", challenge)

    def test_build_authorization_url_contains_required_params(self) -> None:
        url, pending = build_authorization_url(client_id="client123")
        self.assertIn("client_id=client123", url)
        self.assertIn("code_challenge=", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertTrue(pending.code_verifier)
        self.assertTrue(pending.state)

    def test_pending_auth_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pending.json"
            pending = CanvaPendingAuth(
                code_verifier="verifier",
                state="state123",
                redirect_uri="http://127.0.0.1:8765/callback",
            )
            save_pending_auth(path, pending)
            loaded = load_pending_auth(path)
        self.assertEqual(loaded.code_verifier, "verifier")
        self.assertEqual(loaded.state, "state123")

    def test_token_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "token.json"
            token = CanvaToken(
                access_token="access",
                refresh_token="refresh",
                expires_at=1234567890.0,
                scope="design:content:read",
            )
            save_token(path, token)
            loaded = load_token(path)
        self.assertEqual(loaded.access_token, "access")
        self.assertEqual(loaded.refresh_token, "refresh")
        self.assertEqual(loaded.scope, "design:content:read")

    def test_token_from_response(self) -> None:
        token = token_from_response(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
                "scope": "design:content:read",
            }
        )
        self.assertEqual(token.access_token, "access")
        self.assertGreater(token.expires_at, 0)


class CanvaClientTests(unittest.TestCase):
    def _client(self, tmpdir: str) -> CanvaClient:
        token_path = Path(tmpdir) / "token.json"
        save_token(
            token_path,
            CanvaToken(
                access_token="old-access",
                refresh_token="refresh",
                expires_at=0,
                scope="design:content:read",
            ),
        )
        return CanvaClient(
            "client-id",
            "client-secret",
            token_path,
        )

    def test_create_and_poll_export_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            client = self._client(tmpdir)
            with patch.object(client, "ensure_access_token", return_value="access"), patch.object(
                client, "_request"
            ) as request_mock, patch("media_publisher.sources.canva.time.sleep"):
                request_mock.side_effect = [
                    {"job": {"id": "exp1", "status": "in_progress"}},
                    {
                        "job": {
                            "id": "exp1",
                            "status": "success",
                            "urls": ["https://export-download.canva.com/image.png"],
                        }
                    },
                ]
                job = client.export_design("DAVZr1z5464")

        self.assertEqual(job.status, "success")
        self.assertEqual(job.urls[0], "https://export-download.canva.com/image.png")
        self.assertEqual(request_mock.call_count, 2)

    def test_wait_for_export_job_raises_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            client = self._client(tmpdir)
            with patch.object(client, "get_export_job") as get_mock, patch(
                "media_publisher.sources.canva.time.sleep"
            ):
                get_mock.return_value = type(
                    "Job",
                    (),
                    {
                        "id": "exp1",
                        "status": "failed",
                        "urls": (),
                        "error_code": "license_required",
                        "error_message": "Premium license required",
                    },
                )()
                with self.assertRaises(CanvaError):
                    client.wait_for_export_job("exp1")

    def test_refresh_access_token_persists_new_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            client = self._client(tmpdir)
            with patch.object(client, "_token_request") as token_mock:
                token_mock.return_value = CanvaToken(
                    access_token="new-access",
                    refresh_token="new-refresh",
                    expires_at=9999999999.0,
                    scope="design:content:read",
                )
                token = client.refresh_access_token("refresh")

            self.assertEqual(token.access_token, "new-access")
            saved = json.loads((Path(tmpdir) / "token.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["access_token"], "new-access")
            self.assertEqual(saved["refresh_token"], "new-refresh")


class CanvaEnrichmentTests(unittest.TestCase):
    def test_design_id_from_job(self) -> None:
        job = PublishJob(
            title="Sample",
            metadata={METADATA_CANVA_DESIGN_ID: "DAVZr1z5464"},
        )
        self.assertEqual(design_id_from_job(job), "DAVZr1z5464")

    def test_enrich_job_from_canva_downloads_thumbnail(self) -> None:
        job = PublishJob(
            title="Sample",
            airtable_record_id="recABC",
            metadata={METADATA_CANVA_DESIGN_ID: "DAVZr1z5464"},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            download_dir = Path(tmpdir)
            token_path = download_dir / "token.json"
            save_token(
                token_path,
                CanvaToken(
                    access_token="access",
                    refresh_token="refresh",
                    expires_at=9999999999.0,
                ),
            )
            destination = download_dir / "recABC_thumbnail.png"
            with patch("media_publisher.sources.canva.CanvaClient") as client_cls:
                client = client_cls.return_value
                client.download_design_image.return_value = destination

                enriched = enrich_job_from_canva(
                    job,
                    client_id="client-id",
                    client_secret="client-secret",
                    token_path=token_path,
                    download_dir=download_dir,
                )

            self.assertEqual(enriched.thumbnail_path, str(destination))
            self.assertEqual(enriched.metadata[METADATA_CANVA_DESIGN_ID], "DAVZr1z5464")

    def test_enrich_job_from_canva_requires_design_id(self) -> None:
        job = PublishJob(title="Sample")
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(CanvaError):
                enrich_job_from_canva(
                    job,
                    client_id="client-id",
                    client_secret="client-secret",
                    token_path=Path(tmpdir) / "token.json",
                    download_dir=Path(tmpdir),
                )


if __name__ == "__main__":
    unittest.main()
