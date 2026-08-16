from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

from google.oauth2.service_account import Credentials as ServiceAccountCredentials

from catalog_parser.auth import _load_credentials
from catalog_parser.workflow.config import load_catalog_id

REPO_ROOT = Path(__file__).resolve().parents[2]


class AuthServiceAccountTests(unittest.TestCase):
    def test_load_credentials_prefers_service_account(self) -> None:
        sa_creds = object.__new__(ServiceAccountCredentials)
        with patch(
            "catalog_parser.auth.get_service_account_credentials",
            return_value=sa_creds,
        ):
            creds = _load_credentials(
                credentials_path=Path("missing.json"),
                token_path=Path("missing-token.json"),
            )
        self.assertIs(creds, sa_creds)

    def test_print_service_account_email_script_imports(self) -> None:
        self.assertTrue(callable(load_catalog_id))
        script = REPO_ROOT / "scripts" / "catalog" / "print_google_service_account_email.py"
        spec = importlib.util.spec_from_file_location("print_google_service_account_email", script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(callable(module.main))



class AuthServiceAccountTests(unittest.TestCase):
    def test_load_credentials_prefers_service_account(self) -> None:
        sa_creds = object.__new__(ServiceAccountCredentials)
        with patch(
            "catalog_parser.auth.get_service_account_credentials",
            return_value=sa_creds,
        ):
            creds = _load_credentials(
                credentials_path=Path("missing.json"),
                token_path=Path("missing-token.json"),
            )
        self.assertIs(creds, sa_creds)
