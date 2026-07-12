"""Print the Google service account email used for CI / headless Drive access."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from catalog_parser.__main__ import load_env_file
from catalog_parser.auth import get_service_account_email


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")
    email = get_service_account_email()
    if not email:
        print(
            "No service account email found. Set GOOGLE_SERVICE_ACCOUNT_JSON or "
            "GOOGLE_APPLICATION_CREDENTIALS.",
            file=sys.stderr,
        )
        return 1
    print(email)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
