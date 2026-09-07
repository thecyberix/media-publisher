"""CLI: backfill Original Video Thumbnail from package Canva designs.

Scans To do / Translation done / Editing done / Synchronization done and
uploads (or replaces) the Canva design export for every package that has a
Canva link — including canva.link short URLs. When Video caption translated is
empty, also AI-translates the caption from the Canva image.

Usage:
  python scripts/catalog/backfill_canva_thumbnails.py --dry-run
  python scripts/catalog/backfill_canva_thumbnails.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from catalog_parser.airtable import AirtableClient
from catalog_parser.auth import get_docs_service, get_drive_service_noninteractive
from catalog_parser.canva import build_canva_client_from_env, ensure_canva_ready
from catalog_parser.runtime_env import maybe_persist_canva_token, note_canva_token_baseline
from catalog_parser.workflow.backfill_canva_thumbnails import backfill_canva_thumbnails
from media_publisher.config import load_env_file
from media_publisher.sources.airtable import apply_airtable_url_env, parse_airtable_url

load_env_file(PROJECT_ROOT / ".env")


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover Canva links and report what would be uploaded",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Download Canva exports and write Original Video Thumbnail",
    )
    args = parser.parse_args()

    apply_airtable_url_env()
    url = _require_env("AIRTABLE_URL")
    base_id, table_id = parse_airtable_url(url)
    airtable = AirtableClient(
        token=_require_env("AIRTABLE_TOKEN"),
        base_id=base_id,
        table_name=table_id,
    )

    note_canva_token_baseline(PROJECT_ROOT)
    canva_status = ensure_canva_ready(project_root=PROJECT_ROOT)
    if canva_status == "skipped":
        print("ERROR: Canva client credentials not configured", file=sys.stderr)
        return 1
    print(f"Canva: {canva_status}")

    canva_client = build_canva_client_from_env(project_root=PROJECT_ROOT)
    if canva_client is None:
        print("ERROR: Could not build Canva client", file=sys.stderr)
        return 1

    drive = get_drive_service_noninteractive()
    docs = get_docs_service(Path("credentials.json"), Path("token.json"))
    try:
        result = backfill_canva_thumbnails(
            airtable=airtable,
            drive_service=drive,
            docs_service=docs,
            canva_client=canva_client,
            dry_run=bool(args.dry_run),
            project_root=PROJECT_ROOT,
        )
    finally:
        try:
            message = maybe_persist_canva_token(PROJECT_ROOT)
            if message:
                print(message)
        except RuntimeError as exc:
            print(f"Warning: {exc}", file=sys.stderr)

    if result.failed and not args.dry_run:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
