from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from catalog_parser.canva import CanvaClient, build_canva_client_from_env
from catalog_parser.smartcat import DEFAULT_UI_BASE
from catalog_parser.smartcat_web import _looks_like_login_url

EXIT_OK = 0
EXIT_EXPIRED = 1
EXIT_MISSING = 2
EXIT_ERROR = 3


def _assert_not_login_page(*, page_url: str, step: str) -> None:
    if _looks_like_login_url(page_url):
        raise RuntimeError(
            f"Smartcat session expired (redirected to login while checking {step}). "
            "Run locally: python -m catalog_parser --smartcat-login"
        )


def check_smartcat_session(
    *,
    storage_state_path: Path,
    ui_base: str = DEFAULT_UI_BASE,
    probe_project_id: str | None = None,
    timeout_ms: int = 90_000,
) -> None:
    """Validate Smartcat session via cookie HTTP (no Playwright page.goto).

    Full SPA navigation is flaky on GitHub runners (domcontentloaded hangs /
    APIRequestContext aborts while reading HTML). Cookie GET /projects matches
    what ingest uses and is enough to detect expired sessions.
    """
    del timeout_ms  # Kept for call-site compatibility; urllib uses seconds below.
    if not storage_state_path.exists():
        raise FileNotFoundError(
            f"Smartcat session file not found: {storage_state_path}. "
            "Run locally: python -m catalog_parser --smartcat-login"
        )

    from catalog_parser.smartcat import SmartcatError
    from catalog_parser.smartcat_cookie import SmartcatCookieClient

    client = SmartcatCookieClient(
        ui_base=ui_base.rstrip("/"),
        storage_state_path=storage_state_path,
    )
    try:
        # Capture final URL after redirects to detect login pages that still
        # return HTTP 200.
        status, body, final_url = _cookie_get_projects(client)
        if status in {401, 403}:
            raise RuntimeError(
                f"Smartcat session rejected (HTTP {status}). "
                "Run locally: python -m catalog_parser --smartcat-login"
            )
        if status >= 400:
            raise RuntimeError(
                f"Smartcat session check failed with HTTP {status}."
            )
        _assert_not_login_page(page_url=final_url, step="projects")
        snippet = body[:4000].decode("utf-8", errors="replace").casefold()
        if "sign in" in snippet and "password" in snippet:
            raise RuntimeError(
                "Smartcat session expired (login page content on /projects). "
                "Run locally: python -m catalog_parser --smartcat-login"
            )

        if probe_project_id:
            status, body = client.web_request(
                "POST",
                f"/api/Projects/{probe_project_id}/FileItemIds",
                json_body={
                    "isFolderMode": True,
                    "orderBy": 0,
                    "desc": False,
                    "filter": {
                        "searchName": "",
                        "createdByAccountUserIds": [],
                        "targetLanguageIds": [],
                        "documentTargetStatuses": [],
                        "stageNumbersWithNoAssignments": [],
                        "stageNumbersWithIncompleteState": [],
                        "creationDateFrom": None,
                        "creationDateTo": None,
                    },
                },
            )
            if status in {401, 403}:
                raise RuntimeError(
                    f"Smartcat session rejected by project API (HTTP {status}). "
                    "Run locally: python -m catalog_parser --smartcat-login"
                )
            if status >= 400:
                detail = body.decode("utf-8", errors="replace")[:500]
                raise RuntimeError(
                    f"Smartcat project API probe failed with HTTP {status}: {detail}"
                )
    except SmartcatError as exc:
        raise RuntimeError(str(exc)) from exc


def _cookie_get_projects(client: object) -> tuple[int, bytes, str]:
    """GET /projects with cookies; return status, body peek, and final URL."""
    import http.client
    import urllib.error
    import urllib.request

    from catalog_parser.smartcat_cookie import DEFAULT_USER_AGENT, cookies_header

    ui_base = str(getattr(client, "ui_base")).rstrip("/")
    host = str(getattr(client, "_host"))
    cookies = getattr(client, "_cookies")
    url = f"{ui_base}/projects"
    headers = {
        "Cookie": cookies_header(cookies, host=host),
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    request = urllib.request.Request(url, method="GET", headers=headers)

    class _CaptureRedirect(urllib.request.HTTPRedirectHandler):
        def __init__(self) -> None:
            self.final_url = url

        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            self.final_url = newurl
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    capture = _CaptureRedirect()
    opener = urllib.request.build_opener(capture)
    try:
        with opener.open(request, timeout=60) as response:
            final_url = getattr(response, "geturl", lambda: capture.final_url)()
            try:
                # Smartcat sometimes aborts chunked HTML mid-stream on CI/runners.
                # Status + redirect URL are enough; body is only a login sniff.
                body = response.read(16384)
            except http.client.IncompleteRead as exc:
                body = exc.partial or b""
            return response.status, body, str(final_url or capture.final_url)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(16384)
        except http.client.IncompleteRead as read_exc:
            detail = read_exc.partial or b""
        final_url = getattr(exc, "url", None) or capture.final_url
        return exc.code, detail, str(final_url)
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Smartcat authorization check could not reach {url!r}: {exc.reason}. "
            "This is often a transient Smartcat/network issue; re-run the workflow. "
            "If it keeps failing, renew with: python -m catalog_parser --smartcat-login"
        ) from exc


def check_canva_authorization(*, client: CanvaClient) -> str:
    """Validate Canva credentials without refreshing.

    Canva refresh tokens are single-use. Refreshing here and again in the
    orchestrator (or another concurrent job) revokes the whole token lineage.
    """
    if not client.token_path.exists():
        raise FileNotFoundError(
            f"Canva token file not found: {client.token_path}. "
            "Run locally: python scripts/_canva_auth_interactive.py"
        )
    token = client._load_token()
    if token is None:
        raise RuntimeError(
            f"Canva token file is missing or invalid: {client.token_path}. "
            "Run locally: python scripts/_canva_auth_interactive.py"
        )
    if not token.access_token:
        raise RuntimeError(
            "Canva token file is missing access_token. "
            "Run locally: python scripts/_canva_auth_interactive.py"
        )
    if not token.refresh_token:
        raise RuntimeError(
            "Canva token file is missing refresh_token. "
            "Run locally: python scripts/_canva_auth_interactive.py"
        )
    if token.is_expired():
        # Access token expired is fine — the workflow will refresh once later.
        return "access_expired"

    # Probe with the current access token only (never call the refresh endpoint).
    url = f"{client.api_base.rstrip('/')}/users/me"
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {token.access_token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if exc.code in {401, 403}:
            raise RuntimeError(
                "Canva access token was rejected by the API. "
                "Run locally: python scripts/_canva_auth_interactive.py"
            ) from exc
        raise RuntimeError(
            f"Canva authorization probe failed with HTTP {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Canva authorization probe failed: {exc.reason}") from exc
    return "ok"


def _resolve_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    return REPO_ROOT / path


def _canva_is_configured(*, project_root: Path) -> bool:
    client_id = os.getenv("CANVA_CLIENT_ID", "").strip()
    client_secret = os.getenv("CANVA_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return False
    client = build_canva_client_from_env(project_root=project_root)
    return client is not None and client.token_path.exists()


def _classify_runtime_error(message: str) -> int:
    if re.search(r"expired|rejected|login|authorization failed", message, re.IGNORECASE):
        return EXIT_EXPIRED
    return EXIT_ERROR


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Smartcat and Canva credentials before catalog workflows run."
    )
    parser.add_argument(
        "--smartcat-storage-state",
        type=Path,
        default=Path(os.getenv("SMARTCAT_STORAGE_STATE", "smartcat-state.json")),
        help="Path to Playwright storage state (default: smartcat-state.json).",
    )
    parser.add_argument(
        "--ui-base",
        default=os.getenv("SMARTCAT_UI_BASE", DEFAULT_UI_BASE).strip() or DEFAULT_UI_BASE,
        help="Smartcat UI base URL.",
    )
    parser.add_argument(
        "--skip-smartcat-if-missing",
        action="store_true",
        help="Skip Smartcat when no session file exists.",
    )
    parser.add_argument(
        "--skip-canva-if-missing",
        action="store_true",
        help="Skip Canva when client credentials or token file are not configured.",
    )
    parser.add_argument(
        "--probe-project-id",
        default=os.getenv("SMARTCAT_PROBE_PROJECT_ID", "").strip() or None,
        help="Optional Smartcat project UUID for an extra authenticated API probe.",
    )
    args = parser.parse_args()

    exit_code = EXIT_OK
    smartcat_storage_state = _resolve_path(args.smartcat_storage_state)

    if smartcat_storage_state.exists():
        try:
            check_smartcat_session(
                storage_state_path=smartcat_storage_state,
                ui_base=args.ui_base,
                probe_project_id=args.probe_project_id,
            )
        except FileNotFoundError as exc:
            print(exc, file=sys.stderr)
            exit_code = max(exit_code, EXIT_MISSING)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            exit_code = max(exit_code, _classify_runtime_error(str(exc)))
        else:
            print("OK: Smartcat session is valid")
    elif args.skip_smartcat_if_missing:
        print("SKIP: Smartcat session file not configured")
    else:
        print(smartcat_storage_state, file=sys.stderr)
        exit_code = max(exit_code, EXIT_MISSING)

    if _canva_is_configured(project_root=REPO_ROOT):
        client = build_canva_client_from_env(project_root=REPO_ROOT)
        assert client is not None
        try:
            status = check_canva_authorization(client=client)
        except FileNotFoundError as exc:
            print(exc, file=sys.stderr)
            exit_code = max(exit_code, EXIT_MISSING)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            exit_code = max(exit_code, _classify_runtime_error(str(exc)))
        else:
            if status == "access_expired":
                print(
                    "OK: Canva refresh token present "
                    "(access token expired; will refresh once during workflow)"
                )
            else:
                print("OK: Canva authorization is valid")
    elif args.skip_canva_if_missing:
        print("SKIP: Canva credentials or token file not configured")
    else:
        print(
            "Canva client credentials or token file are not configured.",
            file=sys.stderr,
        )
        exit_code = max(exit_code, EXIT_MISSING)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
