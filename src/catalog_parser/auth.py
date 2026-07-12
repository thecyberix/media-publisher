from __future__ import annotations

import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents.readonly",
]
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
DEFAULT_AUTH_PORT = 8080
ENV_SERVICE_ACCOUNT_JSON = "GOOGLE_SERVICE_ACCOUNT_JSON"
ENV_SERVICE_ACCOUNT_FILE = "GOOGLE_SERVICE_ACCOUNT_FILE"
ENV_GOOGLE_APPLICATION_CREDENTIALS = "GOOGLE_APPLICATION_CREDENTIALS"
ENV_DRIVE_TOKEN_PATH = "GOOGLE_OAUTH_DRIVE_TOKEN"
OAUTH_SETUP_HELP = """
Google sign-in failed. Check these items in Google Cloud Console:

1. OAuth consent screen
   - App name and support email are filled in
   - Publishing status is "Testing"
   - YOUR Google account email is listed under Test users

2. APIs
   - Google Sheets API is enabled for the project
   - Google Drive API is enabled for the project
   - Google Docs API is enabled for the project

3. OAuth client
   - Client type must be "Desktop app" (not "Web application")
   - Download a fresh credentials.json after creating the Desktop client

4. If the browser shows redirect_uri_mismatch
   - You likely created a Web client by mistake; create a Desktop app client instead

5. If Google login succeeds but the script hangs or errors afterward
   - Re-run with: python -m catalog_parser --console-auth
   - Paste the authorization code from the browser into the terminal
"""


def inspect_credentials(credentials_path: Path) -> dict[str, str | list[str]]:
    if not credentials_path.exists():
        raise FileNotFoundError(
            f"Missing OAuth client file at {credentials_path}.\n"
            "Download credentials.json from Google Cloud Console "
            "(OAuth client ID -> Desktop app) and place it in the project root."
        )

    data = json.loads(credentials_path.read_text(encoding="utf-8"))
    if "installed" in data:
        client = data["installed"]
        return {
            "client_type": "installed",
            "client_id": client.get("client_id", ""),
            "redirect_uris": client.get("redirect_uris", []),
        }

    if "web" in data:
        client = data["web"]
        raise ValueError(
            "credentials.json is for a Web application OAuth client.\n"
            "This project needs a Desktop app client.\n\n"
            "Fix:\n"
            "  1. Open Google Cloud Console -> APIs & Services -> Credentials\n"
            "  2. Create OAuth client ID -> Application type: Desktop app\n"
            "  3. Download the new JSON and replace credentials.json\n\n"
            f"Current web client redirect URIs: {client.get('redirect_uris', [])}"
        )

    raise ValueError(
        "credentials.json is not a recognized Google OAuth client file. "
        "Expected an 'installed' (Desktop app) section."
    )


def _installed_redirect_uri(credentials_path: Path) -> str:
    info = inspect_credentials(credentials_path)
    redirect_uris = info.get("redirect_uris") or []
    if isinstance(redirect_uris, list) and redirect_uris:
        first = redirect_uris[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    raise RuntimeError(
        "No redirect_uris found in credentials.json. Cannot generate console-auth URL."
    )


def _drive_token_path(token_path: Path) -> Path:
    """
    Use a separate token file for Drive-only operations so we don't clobber
    the main token.json used by Sheets/Docs flows.
    """
    override = os.getenv(ENV_DRIVE_TOKEN_PATH, "").strip()
    if override:
        return Path(override)
    if token_path.suffix.lower() == ".json":
        return token_path.with_name(token_path.stem + ".drive" + token_path.suffix)
    return token_path.with_name(token_path.name + ".drive")


def get_credentials(
    credentials_path: Path,
    token_path: Path,
    *,
    auth_port: int = DEFAULT_AUTH_PORT,
    use_console: bool = False,
    scopes: list[str] | None = None,
) -> Credentials:
    inspect_credentials(credentials_path)

    effective_scopes = scopes or SCOPES
    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), effective_scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                # This commonly happens when SCOPES changed since token creation.
                # Recover by discarding token and re-running the flow.
                if "invalid_scope" in str(exc).lower() and token_path.exists():
                    token_path.unlink(missing_ok=True)
                    creds = None
                else:
                    raise

        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path),
                effective_scopes,
            )
            if use_console:
                redirect_uri = _installed_redirect_uri(credentials_path)
                # Avoid passing redirect_uri both via Flow construction and via
                # authorization_url(kwargs). oauthlib can error with duplicates.
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(credentials_path),
                    effective_scopes,
                    redirect_uri=redirect_uri,
                )
                auth_url, _ = flow.authorization_url(
                    prompt="consent",
                    access_type="offline",
                )
                print("Open this URL in your browser and approve access:")
                print(auth_url)
                print()
                code = input("Paste the authorization code here: ").strip()
                if not code:
                    raise RuntimeError("No authorization code provided.")
                flow.fetch_token(code=code)
                creds = flow.credentials
            else:
                print(
                    f"Opening browser for Google sign-in "
                    f"(callback: http://127.0.0.1:{auth_port}/)..."
                )
                creds = flow.run_local_server(
                    host="127.0.0.1",
                    port=auth_port,
                    prompt="consent",
                    access_type="offline",
                    open_browser=True,
                    authorization_prompt_message=(
                        "Sign in with the Google account that owns the sheet."
                    ),
                )

        if creds is None:
            raise RuntimeError("Google OAuth did not return credentials.")
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return creds


def get_sheets_service(
    credentials_path: Path,
    token_path: Path,
    *,
    auth_port: int = DEFAULT_AUTH_PORT,
    use_console: bool = False,
) -> Resource:
    creds = _load_credentials(
        credentials_path,
        token_path,
        auth_port=auth_port,
        use_console=use_console,
    )
    return build("sheets", "v4", credentials=creds)


def get_drive_service(
    credentials_path: Path,
    token_path: Path,
    *,
    auth_port: int = DEFAULT_AUTH_PORT,
    use_console: bool = False,
) -> Resource:
    drive_token_path = _drive_token_path(token_path)
    creds = _load_credentials(
        credentials_path,
        drive_token_path,
        auth_port=auth_port,
        use_console=use_console,
        scopes=DRIVE_SCOPES,
    )
    return build("drive", "v3", credentials=creds)


def get_docs_service(
    credentials_path: Path,
    token_path: Path,
    *,
    auth_port: int = DEFAULT_AUTH_PORT,
    use_console: bool = False,
) -> Resource:
    creds = _load_credentials(
        credentials_path,
        token_path,
        auth_port=auth_port,
        use_console=use_console,
    )
    return build("docs", "v1", credentials=creds)


def get_service_account_credentials(
    *,
    scopes: list[str] | None = None,
) -> ServiceAccountCredentials | None:
    """
    Returns service account credentials if configured via environment variables.

    Supported env vars (in order):
      - GOOGLE_SERVICE_ACCOUNT_JSON: raw JSON string (or base64-decoded content)
      - GOOGLE_SERVICE_ACCOUNT_FILE: path to JSON file
      - GOOGLE_APPLICATION_CREDENTIALS: path to JSON file (GCP standard)

    If none are set, returns None.
    """
    effective_scopes = scopes or SCOPES

    raw_json = os.getenv(ENV_SERVICE_ACCOUNT_JSON, "").strip()
    if raw_json:
        info = json.loads(raw_json)
        return ServiceAccountCredentials.from_service_account_info(
            info,
            scopes=effective_scopes,
        )

    path = (
        os.getenv(ENV_SERVICE_ACCOUNT_FILE, "").strip()
        or os.getenv(ENV_GOOGLE_APPLICATION_CREDENTIALS, "").strip()
    )
    if path:
        return ServiceAccountCredentials.from_service_account_file(
            path,
            scopes=effective_scopes,
        )

    return None


def get_service_account_email() -> str | None:
    raw_json = os.getenv(ENV_SERVICE_ACCOUNT_JSON, "").strip()
    if raw_json:
        try:
            info = json.loads(raw_json)
        except json.JSONDecodeError:
            info = None
        if isinstance(info, dict):
            email = info.get("client_email")
            if isinstance(email, str) and email.strip():
                return email.strip()

    path = (
        os.getenv(ENV_SERVICE_ACCOUNT_FILE, "").strip()
        or os.getenv(ENV_GOOGLE_APPLICATION_CREDENTIALS, "").strip()
    )
    if path and Path(path).exists():
        try:
            info = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            info = None
        if isinstance(info, dict):
            email = info.get("client_email")
            if isinstance(email, str) and email.strip():
                return email.strip()
    return None


def service_account_email_hint() -> str:
    email = get_service_account_email()
    if email:
        return f" Service account: {email}."
    return ""


def get_drive_service_noninteractive() -> Resource:
    """
    Build a Drive service intended for CI / headless runs.

    This prefers a service account from env; if not present, falls back to the
    interactive desktop OAuth flow (which will likely fail in CI).
    """
    sa = get_service_account_credentials(scopes=DRIVE_SCOPES)
    if sa is not None:
        return build("drive", "v3", credentials=sa)

    credentials_path = Path(os.getenv("GOOGLE_OAUTH_CREDENTIALS", "credentials.json"))
    token_path = Path(os.getenv("GOOGLE_OAUTH_TOKEN", "token.json"))
    return get_drive_service(credentials_path, token_path, use_console=True)


def _load_credentials(
    credentials_path: Path,
    token_path: Path,
    *,
    auth_port: int = DEFAULT_AUTH_PORT,
    use_console: bool = False,
    scopes: list[str] | None = None,
) -> Credentials:
    sa = get_service_account_credentials(scopes=scopes or SCOPES)
    if sa is not None:
        return sa

    try:
        return get_credentials(
            credentials_path,
            token_path,
            auth_port=auth_port,
            use_console=use_console,
            scopes=scopes,
        )
    except Exception as exc:
        raise RuntimeError(f"{exc}\n{OAUTH_SETUP_HELP}") from exc

