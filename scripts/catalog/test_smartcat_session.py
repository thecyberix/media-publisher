from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Smartcat web session is still valid.")
    parser.add_argument(
        "--storage-state",
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
        "--skip-if-missing",
        action="store_true",
        help="Exit 0 when no session file exists (for optional checks).",
    )
    parser.add_argument(
        "--probe-project-id",
        default=os.getenv("SMARTCAT_PROBE_PROJECT_ID", "").strip() or None,
        help="Optional Smartcat project UUID for an extra authenticated API probe.",
    )
    args = parser.parse_args()

    storage_state_path = args.storage_state
    if not storage_state_path.is_absolute():
        storage_state_path = PROJECT_ROOT / storage_state_path

    if not storage_state_path.exists():
        if args.skip_if_missing:
            print("SKIP: Smartcat session file not configured")
            return EXIT_OK
        print(storage_state_path, file=sys.stderr)
        return EXIT_MISSING

    try:
        check_smartcat_session(
            storage_state_path=storage_state_path,
            ui_base=args.ui_base,
            probe_project_id=args.probe_project_id,
        )
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return EXIT_MISSING
    except RuntimeError as exc:
        message = str(exc)
        print(message, file=sys.stderr)
        if re.search(r"expired|rejected|login", message, re.IGNORECASE):
            return EXIT_EXPIRED
        return EXIT_ERROR

    print("OK: Smartcat session is valid")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
