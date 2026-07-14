from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from catalog_parser.canva import CanvaClient, CanvaError, build_canva_client_from_env
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


def _verify_project_api_access(
    request: object,
    *,
    ui_base: str,
    project_id: str,
) -> None:
    """Use the same web API ingest relies on; only 401/403 mean an auth problem."""
    response = request.post(  # type: ignore[attr-defined]
        f"{ui_base}/api/Projects/{project_id}/FileItemIds",
        data=json.dumps(
            {
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
            }
        ),
        headers={"Content-Type": "application/json"},
    )
    if response.status in {401, 403}:
        raise RuntimeError(
            f"Smartcat session rejected by project API (HTTP {response.status}). "
            "Run locally: python -m catalog_parser --smartcat-login"
        )


def check_smartcat_session(
    *,
    storage_state_path: Path,
    ui_base: str = DEFAULT_UI_BASE,
    probe_project_id: str | None = None,
    timeout_ms: int = 90_000,
) -> None:
    if not storage_state_path.exists():
        raise FileNotFoundError(
            f"Smartcat session file not found: {storage_state_path}. "
            "Run locally: python -m catalog_parser --smartcat-login"
        )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is required. Install with: pip install playwright && playwright install chromium"
        ) from exc

    ui_base = ui_base.rstrip("/")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(storage_state_path))
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            page.goto(ui_base, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(3000)
            _assert_not_login_page(page_url=page.url, step="home")

            page.goto(f"{ui_base}/projects", wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(3000)
            _assert_not_login_page(page_url=page.url, step="projects")

            if probe_project_id:
                _verify_project_api_access(
                    context.request,
                    ui_base=ui_base,
                    project_id=probe_project_id,
                )
        finally:
            context.close()
            browser.close()


def check_canva_authorization(*, client: CanvaClient) -> None:
    if not client.token_path.exists():
        raise FileNotFoundError(
            f"Canva token file not found: {client.token_path}. "
            "Run locally: python scripts/_canva_auth_interactive.py"
        )
    try:
        client.get_access_token()
    except CanvaError as exc:
        raise RuntimeError(
            f"Canva authorization failed: {exc}. "
            "Run locally: python scripts/_canva_auth_interactive.py"
        ) from exc


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
            check_canva_authorization(client=client)
        except FileNotFoundError as exc:
            print(exc, file=sys.stderr)
            exit_code = max(exit_code, EXIT_MISSING)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            exit_code = max(exit_code, _classify_runtime_error(str(exc)))
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
