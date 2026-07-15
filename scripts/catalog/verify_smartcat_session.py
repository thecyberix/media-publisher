"""Verify Smartcat web session is still valid (CLI helper for local/CI checks)."""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_authorization import (
    EXIT_ERROR,
    EXIT_EXPIRED,
    EXIT_MISSING,
    EXIT_OK,
    REPO_ROOT,
    check_smartcat_session,
)


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
        default=os.getenv("SMARTCAT_UI_BASE", "https://ea.smartcat.com").strip()
        or "https://ea.smartcat.com",
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
        storage_state_path = REPO_ROOT / storage_state_path

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
