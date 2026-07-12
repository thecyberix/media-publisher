from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from google.oauth2.service_account import Credentials as ServiceAccountCredentials

from catalog_parser.auth import _load_credentials


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
