from __future__ import annotations

import argparse
import sys
from pathlib import Path

from media_publisher.config import load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract publishing metadata from Airtable, HappyScribe, and Canva, "
            "then publish to YouTube, Facebook, and Instagram."
        )
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate required environment variables and exit.",
    )
    return parser


def main() -> int:
    settings = load_settings(PROJECT_ROOT)
    parser = build_parser()
    args = parser.parse_args()

    if args.check_config:
        missing = []
        if not settings.airtable_token:
            missing.append("AIRTABLE_TOKEN")
        if not settings.airtable_base_id:
            missing.append("AIRTABLE_BASE_ID")
        if not settings.airtable_table_name:
            missing.append("AIRTABLE_TABLE_NAME")
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        print("Required Airtable settings are present.")
        print("Optional integrations:")
        print(f"  HappyScribe: {'yes' if settings.happyscribe_api_key else 'no'}")
        print(f"  Canva: {'yes' if settings.canva_client_id else 'no'}")
        print(f"  Meta: {'yes' if settings.meta_access_token else 'no'}")
        return 0

    parser.error("No action specified. Try --check-config")
    return 2


if __name__ == "__main__":
    sys.exit(main())
