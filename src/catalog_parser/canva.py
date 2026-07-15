from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

from catalog_parser.runtime_env import CANVA_TOKEN_RELATIVE_PATH

DEFAULT_API_BASE = "https://api.canva.com/rest/v1"
DEFAULT_AUTH_BASE = "https://www.canva.com/api"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"
DEFAULT_TOKEN_FILENAME = CANVA_TOKEN_RELATIVE_PATH
DEFAULT_PENDING_AUTH_FILENAME = "credentials/canva-auth-pending.json"
DEFAULT_AUTH_PORT = 8765
DEFAULT_SCOPES = (
    "design:content:read",
    "design:meta:read",
)
EXPORT_POLL_INITIAL_DELAY_SECONDS = 0.5
EXPORT_POLL_MAX_DELAY_SECONDS = 10.0
EXPORT_POLL_INCREASE_FACTOR = 1.6
EXPORT_POLL_TIMEOUT_SECONDS = 120.0

CANVA_DESIGN_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?canva\.com/design/(?P<design_id>[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)


class CanvaError(RuntimeError):
    pass


@dataclass(frozen=True)
class CanvaPendingAuth:
    code_verifier: str
    state: str
    redirect_uri: str

    def to_json(self) -> dict[str, str]:
        return {
            "code_verifier": self.code_verifier,
            "state": self.state,
            "redirect_uri": self.redirect_uri,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CanvaPendingAuth:
        return cls(
            code_verifier=str(data.get("code_verifier", "")).strip(),
            state=str(data.get("state", "")).strip(),
            redirect_uri=str(data.get("redirect_uri", "")).strip(),
        )


@dataclass(frozen=True)
class CanvaToken:
    access_token: str
    refresh_token: str | None
    token_type: str
    expires_at: float | None
    scope: str | None = None

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() >= self.expires_at - 30

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "scope": self.scope,
        }
        return payload

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CanvaToken:
        return cls(
            access_token=str(data.get("access_token", "")).strip(),
            refresh_token=(
                str(data["refresh_token"]).strip()
                if data.get("refresh_token")
                else None
            ),
            token_type=str(data.get("token_type", "Bearer")).strip() or "Bearer",
            expires_at=(
                float(data["expires_at"])
                if data.get("expires_at") is not None
                else None
            ),
            scope=str(data["scope"]).strip() if data.get("scope") else None,
        )


def parse_canva_design_url(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    match = CANVA_DESIGN_URL_PATTERN.search(value.strip())
    if not match:
        return None
    return match.group("design_id")


def extract_canva_design_url(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    match = CANVA_DESIGN_URL_PATTERN.search(value.strip())
    if not match:
        return None
    return match.group(0)


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    credentials = f"{client_id}:{client_secret}".encode("utf-8")
    encoded = base64.b64encode(credentials).decode("ascii")
    return f"Basic {encoded}"


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scopes: tuple[str, ...] = DEFAULT_SCOPES,
    auth_base: str = DEFAULT_AUTH_BASE,
) -> str:
    query = urllib.parse.urlencode(
        {
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "scope": " ".join(scopes),
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{auth_base.rstrip('/')}/oauth/authorize?{query}"


class CanvaClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        token_path: Path,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        pending_auth_path: Path | None = None,
        api_base: str = DEFAULT_API_BASE,
        auth_base: str = DEFAULT_AUTH_BASE,
        scopes: tuple[str, ...] = DEFAULT_SCOPES,
    ) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.token_path = token_path
        self.pending_auth_path = pending_auth_path or (
            token_path.parent / "canva-auth-pending.json"
        )
        self.redirect_uri = redirect_uri.strip()
        self.api_base = api_base.rstrip("/")
        self.auth_base = auth_base.rstrip("/")
        self.scopes = scopes
        if not self.client_id:
            raise CanvaError("CANVA_CLIENT_ID is required")
        if not self.client_secret:
            raise CanvaError("CANVA_CLIENT_SECRET is required")

    def _load_token(self) -> CanvaToken | None:
        if not self.token_path.exists():
            return None
        data = json.loads(self.token_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        token = CanvaToken.from_json(data)
        if not token.access_token:
            return None
        return token

    def _save_token(self, token: CanvaToken) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(
            json.dumps(token.to_json(), indent=2),
            encoding="utf-8",
        )

    def _save_pending_auth(self, pending: CanvaPendingAuth) -> None:
        self.pending_auth_path.parent.mkdir(parents=True, exist_ok=True)
        self.pending_auth_path.write_text(
            json.dumps(pending.to_json(), indent=2),
            encoding="utf-8",
        )

    def _load_pending_auth(self) -> CanvaPendingAuth:
        if not self.pending_auth_path.exists():
            raise CanvaError(
                f"No pending Canva auth state found at {self.pending_auth_path}. "
                "Run: python -m catalog_parser --canva-auth"
            )
        data = json.loads(self.pending_auth_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise CanvaError("Pending Canva auth state is invalid")
        pending = CanvaPendingAuth.from_json(data)
        if not pending.code_verifier or not pending.redirect_uri:
            raise CanvaError("Pending Canva auth state is incomplete")
        return pending

    def _clear_pending_auth(self) -> None:
        self.pending_auth_path.unlink(missing_ok=True)

    def _exchange_token(
        self,
        body: dict[str, str],
        *,
        fallback_refresh_token: str | None = None,
    ) -> CanvaToken:
        request = urllib.request.Request(
            f"{self.api_base}/oauth/token",
            data=urllib.parse.urlencode(body).encode("utf-8"),
            method="POST",
        )
        request.add_header(
            "Authorization",
            _basic_auth_header(self.client_id, self.client_secret),
        )
        request.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise CanvaError(
                f"Canva token exchange failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise CanvaError(f"Canva token exchange failed: {exc.reason}") from exc

        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise CanvaError("Canva token exchange returned no access token")

        expires_in = payload.get("expires_in")
        expires_at = (
            time.time() + float(expires_in)
            if expires_in is not None
            else None
        )
        refresh_token = (
            str(payload["refresh_token"])
            if payload.get("refresh_token")
            else fallback_refresh_token
        )
        return CanvaToken(
            access_token=str(payload["access_token"]),
            refresh_token=refresh_token,
            token_type=str(payload.get("token_type", "Bearer")),
            expires_at=expires_at,
            scope=str(payload["scope"]) if payload.get("scope") else None,
        )

    def _refresh_access_token(self, refresh_token: str) -> CanvaToken:
        return self._exchange_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            fallback_refresh_token=refresh_token,
        )

    def get_access_token(self) -> str:
        token = self._load_token()
        if token is None:
            raise CanvaError(
                f"No Canva token found at {self.token_path}. "
                "Run: python -m catalog_parser --canva-auth, then --canva-auth-code"
            )
        if token.is_expired():
            if not token.refresh_token:
                raise CanvaError(
                    "Canva access token expired and no refresh token is available. "
                    "Run: python -m catalog_parser --canva-auth, then --canva-auth-code"
                )
            token = self._refresh_access_token(token.refresh_token)
            self._save_token(token)
        return token.access_token

    def start_auth_flow(self, *, open_browser: bool = False) -> str:
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = _pkce_challenge(code_verifier)
        state = secrets.token_urlsafe(32)
        pending = CanvaPendingAuth(
            code_verifier=code_verifier,
            state=state,
            redirect_uri=self.redirect_uri,
        )
        self._save_pending_auth(pending)
        auth_url = build_authorization_url(
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            state=state,
            code_challenge=code_challenge,
            scopes=self.scopes,
            auth_base=self.auth_base,
        )
        print("Open this URL in your browser and approve access:")
        print(auth_url)
        print()
        print(
            "After approval, copy the authorization code from the redirect URL and run:"
        )
        print("  python -m catalog_parser --canva-auth-code <authorization-code>")
        if open_browser:
            webbrowser.open(auth_url, new=1, autoraise=True)
        return auth_url

    def complete_auth_flow(self, authorization_code: str) -> CanvaToken:
        authorization_code = authorization_code.strip()
        if not authorization_code:
            raise CanvaError("Authorization code is required")

        pending = self._load_pending_auth()
        token = self._exchange_token(
            {
                "grant_type": "authorization_code",
                "code": authorization_code,
                "code_verifier": pending.code_verifier,
                "redirect_uri": pending.redirect_uri,
            }
        )
        self._save_token(token)
        self._clear_pending_auth()
        print(f"Saved Canva token to {self.token_path}")
        return token

    def login_interactive(
        self,
        *,
        auth_port: int = DEFAULT_AUTH_PORT,
        open_browser: bool = True,
    ) -> CanvaToken:
        redirect_uri = f"http://127.0.0.1:{auth_port}/oauth/redirect"
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = _pkce_challenge(code_verifier)
        state = secrets.token_urlsafe(32)
        auth_url = build_authorization_url(
            client_id=self.client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            scopes=self.scopes,
            auth_base=self.auth_base,
        )

        result: dict[str, str | None] = {
            "code": None,
            "state": None,
            "error": None,
        }

        class OAuthHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/oauth/redirect":
                    self.send_response(404)
                    self.end_headers()
                    return

                params = urllib.parse.parse_qs(parsed.query)
                result["code"] = params.get("code", [None])[0]
                result["state"] = params.get("state", [None])[0]
                result["error"] = params.get("error", [None])[0]

                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                if result["error"]:
                    body = (
                        "<h1>Canva authorization failed</h1>"
                        f"<p>{result['error']}</p>"
                    )
                else:
                    body = (
                        "<h1>Canva authorization complete</h1>"
                        "<p>You can close this tab and return to the terminal.</p>"
                    )
                self.wfile.write(body.encode("utf-8"))

            def log_message(self, format: str, *args: Any) -> None:
                return

        server = HTTPServer(("127.0.0.1", auth_port), OAuthHandler)
        thread = Thread(target=server.handle_request, daemon=True)
        thread.start()

        print("Open this URL to authorize Canva:")
        print(auth_url)
        if open_browser:
            webbrowser.open(auth_url, new=1, autoraise=True)
        print(f"Waiting for OAuth callback on {redirect_uri} ...")

        thread.join(timeout=300)
        server.server_close()

        if result["error"]:
            raise CanvaError(f"Canva authorization failed: {result['error']}")
        if result["state"] != state:
            raise CanvaError("Canva authorization failed: state mismatch")
        if not result["code"]:
            raise CanvaError("Canva authorization failed: no authorization code")

        token = self._exchange_token(
            {
                "grant_type": "authorization_code",
                "code": str(result["code"]),
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            }
        )
        self._save_token(token)
        print(f"Saved Canva token to {self.token_path}")
        return token

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.api_base}{path}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.get_access_token()}")
        if body is not None:
            request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise CanvaError(
                f"Canva {method} {path} failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise CanvaError(f"Canva request failed: {exc.reason}") from exc

        if not payload:
            return {}
        parsed = json.loads(payload.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise CanvaError(f"Unexpected Canva response for {method} {path}")
        return parsed

    def create_design_export_job(
        self,
        design_id: str,
        *,
        file_type: str = "jpg",
        quality: int = 90,
    ) -> str:
        response = self._request(
            "POST",
            "/exports",
            body={
                "design_id": design_id,
                "format": {
                    "type": file_type,
                    "quality": quality,
                },
            },
        )
        job = response.get("job")
        if not isinstance(job, dict):
            raise CanvaError("Canva export response is missing job metadata")
        export_id = job.get("id")
        if not isinstance(export_id, str) or not export_id:
            raise CanvaError("Canva export response is missing job id")
        return export_id

    def get_design_export_job(self, export_id: str) -> dict[str, Any]:
        response = self._request("GET", f"/exports/{export_id}")
        job = response.get("job")
        if not isinstance(job, dict):
            raise CanvaError(f"Canva export job {export_id!r} has no job payload")
        return job

    def export_design_image_url(
        self,
        design_id: str,
        *,
        file_type: str = "jpg",
        quality: int = 90,
        timeout_seconds: float = EXPORT_POLL_TIMEOUT_SECONDS,
    ) -> str:
        export_id = self.create_design_export_job(
            design_id,
            file_type=file_type,
            quality=quality,
        )
        delay_seconds = EXPORT_POLL_INITIAL_DELAY_SECONDS
        started = time.time()

        while True:
            job = self.get_design_export_job(export_id)
            status = str(job.get("status", "")).casefold()
            if status == "success":
                urls = job.get("urls")
                if not isinstance(urls, list) or not urls:
                    raise CanvaError(
                        f"Canva export job {export_id!r} succeeded without URLs"
                    )
                first_url = urls[0]
                if not isinstance(first_url, str) or not first_url.strip():
                    raise CanvaError(
                        f"Canva export job {export_id!r} returned an invalid URL"
                    )
                return first_url.strip()
            if status == "failed":
                error = job.get("error")
                raise CanvaError(
                    f"Canva export job {export_id!r} failed: {error!r}"
                )
            if time.time() - started > timeout_seconds:
                raise CanvaError(
                    f"Timed out waiting for Canva export job {export_id!r}"
                )
            time.sleep(delay_seconds)
            delay_seconds = min(
                delay_seconds * EXPORT_POLL_INCREASE_FACTOR,
                EXPORT_POLL_MAX_DELAY_SECONDS,
            )

    def export_design_url_from_link(self, canva_url: str) -> str:
        design_id = parse_canva_design_url(canva_url)
        if design_id is None:
            raise CanvaError(f"Could not parse Canva design id from {canva_url!r}")
        return self.export_design_image_url(design_id)


def build_canva_client_from_env(
    *,
    project_root: Path,
    token_filename: str = DEFAULT_TOKEN_FILENAME,
) -> CanvaClient | None:
    client_id = os.getenv("CANVA_CLIENT_ID", "").strip()
    client_secret = os.getenv("CANVA_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None

    token_setting = (
        os.getenv("CANVA_TOKEN_PATH", "").strip()
        or os.getenv("CANVA_TOKEN", "").strip()
        or token_filename
    )
    token_path = Path(token_setting)
    if not token_path.is_absolute():
        token_path = project_root / token_path
    pending_auth_path = Path(
        os.getenv(
            "CANVA_PENDING_AUTH_PATH",
            str(project_root / DEFAULT_PENDING_AUTH_FILENAME),
        )
    )
    redirect_uri = (
        os.getenv("CANVA_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip()
        or DEFAULT_REDIRECT_URI
    )
    api_base = (
        os.getenv("CANVA_API_BASE", DEFAULT_API_BASE).strip() or DEFAULT_API_BASE
    )
    auth_base = (
        os.getenv("CANVA_AUTH_BASE", DEFAULT_AUTH_BASE).strip() or DEFAULT_AUTH_BASE
    )
    return CanvaClient(
        client_id=client_id,
        client_secret=client_secret,
        token_path=token_path,
        pending_auth_path=pending_auth_path,
        redirect_uri=redirect_uri,
        api_base=api_base,
        auth_base=auth_base,
    )
