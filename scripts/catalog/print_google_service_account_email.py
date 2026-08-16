"""Print the Google service account email used for CI / headless Drive access."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from media_publisher.config import load_env_file
from catalog_parser.auth import ENV_SERVICE_ACCOUNT_JSON, get_service_account_email
from catalog_parser.runtime_env import materialize_credentials

DEFAULT_SERVICE_ACCOUNT_PATH = REPO_ROOT / "credentials" / "google-sheets-service-account.json"


def main() -> int:
    load_env_file(REPO_ROOT / ".env")
    materialize_credentials(REPO_ROOT)

    raw_json = os.getenv(ENV_SERVICE_ACCOUNT_JSON, "").strip()
    if raw_json:
        try:
            json.loads(raw_json)
        except json.JSONDecodeError as exc:
            print(
                f"GOOGLE_SERVICE_ACCOUNT_JSON is set but is not valid JSON: {exc}",
                file=sys.stderr,
            )
            return 1

    if DEFAULT_SERVICE_ACCOUNT_PATH.is_file() and not os.getenv(
        "GOOGLE_APPLICATION_CREDENTIALS", ""
    ).strip():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(DEFAULT_SERVICE_ACCOUNT_PATH)

    email = get_service_account_email()
    if not email:
        print(
            "No service account email found. Set GOOGLE_SERVICE_ACCOUNT_JSON to the full "
            "service account key JSON (not a file path), or place the key at "
            f"{DEFAULT_SERVICE_ACCOUNT_PATH}.",
            file=sys.stderr,
        )
        if raw_json:
            print(
                f"GOOGLE_SERVICE_ACCOUNT_JSON is present ({len(raw_json)} chars) but "
                "client_email could not be read.",
                file=sys.stderr,
            )
        return 1
    print(email)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
