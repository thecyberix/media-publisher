from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_CATALOG = REPO_ROOT / "scripts" / "catalog"
sys.path.insert(0, str(SCRIPTS_CATALOG))

import check_authorization  # noqa: E402


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

    def test_check_canva_authorization_refreshes_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "canva-token.json"
            token_path.write_text("{}", encoding="utf-8")
            client = MagicMock()
            client.token_path = token_path

            check_authorization.check_canva_authorization(client=client)

            client.get_access_token.assert_called_once()

    def test_check_canva_authorization_wraps_canva_error(self) -> None:
        from catalog_parser.canva import CanvaError

        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "canva-token.json"
            token_path.write_text("{}", encoding="utf-8")
            client = MagicMock()
            client.token_path = token_path
            client.get_access_token.side_effect = CanvaError("refresh token revoked")

            with self.assertRaisesRegex(RuntimeError, "Canva authorization failed"):
                check_authorization.check_canva_authorization(client=client)

    @patch.object(check_authorization, "check_smartcat_session")
    @patch.object(check_authorization, "_canva_is_configured", return_value=False)
    def test_main_skips_both_when_missing(
        self,
        _canva_configured: MagicMock,
        _smartcat_check: MagicMock,
    ) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "check_authorization.py",
                "--skip-smartcat-if-missing",
                "--skip-canva-if-missing",
            ],
        ):
            exit_code = check_authorization.main()

        self.assertEqual(exit_code, check_authorization.EXIT_OK)
        _smartcat_check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
