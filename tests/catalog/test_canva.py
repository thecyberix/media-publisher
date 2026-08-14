from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from catalog_parser.canva import (
    CanvaClient,
    CanvaError,
    CanvaToken,
    build_authorization_url,
    ensure_canva_ready,
    parse_canva_design_url,
)


class CanvaParsingTests(unittest.TestCase):
    def test_parse_canva_design_url(self) -> None:
        self.assertEqual(
            parse_canva_design_url(
                "https://www.canva.com/design/DAF123abc/edit?utm_source=share"
            ),
            "DAF123abc",
        )
        self.assertIsNone(parse_canva_design_url("https://example.com/design/abc"))


class CanvaClientTests(unittest.TestCase):
    def test_build_authorization_url_contains_pkce_params(self) -> None:
        url = build_authorization_url(
            client_id="client-id",
            redirect_uri="http://127.0.0.1:8765/oauth/redirect",
            state="state-value",
            code_challenge="challenge-value",
        )
        self.assertIn("code_challenge=challenge-value", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("client_id=client-id", url)

    def test_get_access_token_refreshes_expired_token(self) -> None:
        token_path = Path(self._testMethodName) / "canva-token.json"
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(
            json.dumps(
                {
                    "access_token": "old-access",
                    "refresh_token": "refresh-token",
                    "token_type": "Bearer",
                    "expires_at": 0,
                }
            ),
            encoding="utf-8",
        )
        client = CanvaClient(
            client_id="client-id",
            client_secret="client-secret",
            token_path=token_path,
        )

        refreshed = CanvaToken(
            access_token="new-access",
            refresh_token="refresh-token",
            token_type="Bearer",
            expires_at=9999999999,
        )
        with patch.object(client, "_refresh_access_token", return_value=refreshed) as refresh_mock:
            self.assertEqual(client.get_access_token(), "new-access")
        refresh_mock.assert_called_once_with("refresh-token")
        saved = json.loads(token_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["access_token"], "new-access")

    def test_complete_auth_flow_uses_pending_pkce_state(self) -> None:
        token_path = Path(self._testMethodName) / "canva-token.json"
        pending_path = Path(self._testMethodName) / "canva-auth-pending.json"
        token_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            json.dumps(
                {
                    "code_verifier": "verifier-123",
                    "state": "state-123",
                    "redirect_uri": "http://127.0.0.1:8765/oauth/redirect",
                }
            ),
            encoding="utf-8",
        )
        client = CanvaClient(
            client_id="client-id",
            client_secret="client-secret",
            token_path=token_path,
            pending_auth_path=pending_path,
            redirect_uri="http://127.0.0.1:8765/oauth/redirect",
        )
        saved = CanvaToken(
            access_token="new-access",
            refresh_token="refresh-token",
            token_type="Bearer",
            expires_at=9999999999,
        )
        with patch.object(client, "_exchange_token", return_value=saved) as exchange_mock:
            client.complete_auth_flow("auth-code-123")

        exchange_mock.assert_called_once_with(
            {
                "grant_type": "authorization_code",
                "code": "auth-code-123",
                "code_verifier": "verifier-123",
                "redirect_uri": "http://127.0.0.1:8765/oauth/redirect",
            }
        )
        self.assertFalse(pending_path.exists())
        stored = json.loads(token_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["access_token"], "new-access")

    def test_start_auth_flow_writes_pending_state(self) -> None:
        token_path = Path(self._testMethodName) / "canva-token.json"
        pending_path = Path(self._testMethodName) / "canva-auth-pending.json"
        token_path.parent.mkdir(parents=True, exist_ok=True)
        client = CanvaClient(
            client_id="client-id",
            client_secret="client-secret",
            token_path=token_path,
            pending_auth_path=pending_path,
            redirect_uri="http://127.0.0.1:8765/oauth/redirect",
        )
        auth_url = client.start_auth_flow(open_browser=False)
        self.assertIn("client_id=client-id", auth_url)
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        self.assertTrue(pending["code_verifier"])
        self.assertTrue(pending["state"])
        self.assertEqual(
            pending["redirect_uri"],
            "http://127.0.0.1:8765/oauth/redirect",
        )

    def test_export_design_image_url_returns_first_success_url(self) -> None:
        client = CanvaClient(
            client_id="client-id",
            client_secret="client-secret",
            token_path=Path("unused.json"),
        )
        with patch.object(client, "create_design_export_job", return_value="job-1"):
            with patch.object(
                client,
                "get_design_export_job",
                return_value={"status": "success", "urls": ["https://cdn.example/a.jpg"]},
            ):
                self.assertEqual(
                    client.export_design_image_url("DAF123abc"),
                    "https://cdn.example/a.jpg",
                )

    def test_ensure_ready_probes_without_refresh_when_access_valid(self) -> None:
        token_path = Path(self._testMethodName) / "canva-token.json"
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(
            json.dumps(
                {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "token_type": "Bearer",
                    "expires_at": time.time() + 3600,
                }
            ),
            encoding="utf-8",
        )
        client = CanvaClient(
            client_id="client-id",
            client_secret="client-secret",
            token_path=token_path,
        )
        with (
            patch.object(client, "_probe_users_me") as probe_mock,
            patch.object(client, "_refresh_access_token") as refresh_mock,
        ):
            self.assertEqual(client.ensure_ready(), "ok")
        probe_mock.assert_called_once_with("access")
        refresh_mock.assert_not_called()

    def test_ensure_ready_refreshes_expired_token_then_probes(self) -> None:
        token_path = Path(self._testMethodName) / "canva-token.json"
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(
            json.dumps(
                {
                    "access_token": "old-access",
                    "refresh_token": "refresh",
                    "token_type": "Bearer",
                    "expires_at": 0,
                }
            ),
            encoding="utf-8",
        )
        client = CanvaClient(
            client_id="client-id",
            client_secret="client-secret",
            token_path=token_path,
        )
        refreshed = CanvaToken(
            access_token="new-access",
            refresh_token="new-refresh",
            token_type="Bearer",
            expires_at=time.time() + 3600,
        )
        with (
            patch.object(client, "_refresh_access_token", return_value=refreshed) as refresh_mock,
            patch.object(client, "_probe_users_me") as probe_mock,
        ):
            self.assertEqual(client.ensure_ready(), "refreshed")
        refresh_mock.assert_called_once_with("refresh")
        probe_mock.assert_called_once_with("new-access")
        saved = json.loads(token_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["access_token"], "new-access")

    def test_ensure_ready_refreshes_once_when_unexpired_probe_fails(self) -> None:
        token_path = Path(self._testMethodName) / "canva-token.json"
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(
            json.dumps(
                {
                    "access_token": "stale-access",
                    "refresh_token": "refresh",
                    "token_type": "Bearer",
                    "expires_at": time.time() + 3600,
                }
            ),
            encoding="utf-8",
        )
        client = CanvaClient(
            client_id="client-id",
            client_secret="client-secret",
            token_path=token_path,
        )
        refreshed = CanvaToken(
            access_token="new-access",
            refresh_token="new-refresh",
            token_type="Bearer",
            expires_at=time.time() + 3600,
        )
        with (
            patch.object(client, "_refresh_access_token", return_value=refreshed) as refresh_mock,
            patch.object(
                client,
                "_probe_users_me",
                side_effect=[CanvaError("rejected"), None],
            ) as probe_mock,
        ):
            self.assertEqual(client.ensure_ready(), "refreshed")
        refresh_mock.assert_called_once_with("refresh")
        self.assertEqual(
            [call.args[0] for call in probe_mock.call_args_list],
            ["stale-access", "new-access"],
        )

    def test_ensure_canva_ready_skips_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {"CANVA_CLIENT_ID": "", "CANVA_CLIENT_SECRET": ""},
                clear=False,
            ):
                self.assertEqual(ensure_canva_ready(project_root=Path(tmp)), "skipped")

    def test_ensure_canva_ready_persists_after_refresh(self) -> None:
        client = MagicMock()
        client.ensure_ready.return_value = "refreshed"
        with (
            patch("catalog_parser.canva.build_canva_client_from_env", return_value=client),
            patch("catalog_parser.runtime_env.maybe_persist_canva_token", return_value=None) as persist,
        ):
            self.assertEqual(ensure_canva_ready(project_root=Path(".")), "refreshed")
        persist.assert_called_once_with(Path("."))


if __name__ == "__main__":
    unittest.main()
