from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_CATALOG = REPO_ROOT / "scripts" / "catalog"
sys.path.insert(0, str(SCRIPTS_CATALOG))

import check_authorization  # noqa: E402
from catalog_parser.canva import CanvaToken  # noqa: E402


class CheckAuthorizationTests(unittest.TestCase):
    def test_canva_is_configured_requires_client_credentials_and_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_path = root / "credentials" / "canva-token.json"
            token_path.parent.mkdir(parents=True)
            token_path.write_text("{}", encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "CANVA_CLIENT_ID": "client-id",
                    "CANVA_CLIENT_SECRET": "client-secret",
                    "CANVA_TOKEN": str(token_path),
                },
                clear=False,
            ):
                self.assertTrue(check_authorization._canva_is_configured(project_root=root))

            with patch.dict(
                os.environ,
                {"CANVA_CLIENT_ID": "", "CANVA_CLIENT_SECRET": ""},
                clear=False,
            ):
                self.assertFalse(check_authorization._canva_is_configured(project_root=root))

    def test_check_canva_authorization_skips_refresh_when_access_expired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "canva-token.json"
            token_path.write_text("{}", encoding="utf-8")
            client = MagicMock()
            client.token_path = token_path
            client._load_token.return_value = CanvaToken(
                access_token="access",
                refresh_token="refresh",
                token_type="Bearer",
                expires_at=time.time() - 100,
            )

            status = check_authorization.check_canva_authorization(client=client)

            self.assertEqual(status, "access_expired")
            client.get_access_token.assert_not_called()

    def test_check_canva_authorization_probes_without_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "canva-token.json"
            token_path.write_text("{}", encoding="utf-8")
            client = MagicMock()
            client.token_path = token_path
            client.api_base = "https://api.canva.com/rest/v1"
            client._load_token.return_value = CanvaToken(
                access_token="access",
                refresh_token="refresh",
                token_type="Bearer",
                expires_at=time.time() + 3600,
            )

            with patch("check_authorization.urllib.request.urlopen") as urlopen:
                response = MagicMock()
                response.read.return_value = b"{}"
                response.__enter__.return_value = response
                response.__exit__.return_value = False
                urlopen.return_value = response

                status = check_authorization.check_canva_authorization(client=client)

            self.assertEqual(status, "ok")
            client.get_access_token.assert_not_called()
            request = urlopen.call_args.args[0]
            self.assertEqual(
                request.get_header("Authorization"),
                "Bearer access",
            )

    def test_check_canva_authorization_rejects_unauthorized_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "canva-token.json"
            token_path.write_text("{}", encoding="utf-8")
            client = MagicMock()
            client.token_path = token_path
            client.api_base = "https://api.canva.com/rest/v1"
            client._load_token.return_value = CanvaToken(
                access_token="access",
                refresh_token="refresh",
                token_type="Bearer",
                expires_at=time.time() + 3600,
            )

            error = urllib.error.HTTPError(
                url="https://api.canva.com/rest/v1/users/me",
                code=401,
                msg="Unauthorized",
                hdrs=None,
                fp=None,
            )
            error.read = MagicMock(return_value=b"unauthorized")  # type: ignore[method-assign]

            with patch(
                "check_authorization.urllib.request.urlopen",
                side_effect=error,
            ):
                with self.assertRaisesRegex(RuntimeError, "rejected"):
                    check_authorization.check_canva_authorization(client=client)

    @patch.object(check_authorization, "check_smartcat_session")
    @patch.object(check_authorization, "_canva_is_configured", return_value=False)
    def test_main_skips_both_when_missing(
        self,
        _canva_configured: MagicMock,
        _smartcat_check: MagicMock,
    ) -> None:
        missing_state = Path(tempfile.mkdtemp()) / "missing-smartcat-state.json"
        with patch.object(
            sys,
            "argv",
            [
                "check_authorization.py",
                "--smartcat-storage-state",
                str(missing_state),
                "--skip-smartcat-if-missing",
                "--skip-canva-if-missing",
            ],
        ):
            exit_code = check_authorization.main()

        self.assertEqual(exit_code, check_authorization.EXIT_OK)
        _smartcat_check.assert_not_called()

    def test_cookie_get_projects_maps_url_errors(self) -> None:
        client = MagicMock()
        client.ui_base = "https://ea.smartcat.com"
        client._host = "ea.smartcat.com"
        client._cookies = [{"name": "session", "value": "x", "domain": "ea.smartcat.com"}]

        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("timed out")
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "could not reach"):
                check_authorization._cookie_get_projects(client)


if __name__ == "__main__":
    unittest.main()
