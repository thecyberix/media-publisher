"""Generate TN thumbnails for specific catalog titles."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from media_publisher.config import load_settings
from media_publisher.sources.airtable import AirtableClient, catalog_title
from media_publisher.sources.google_drive import GoogleDriveClient
from media_publisher.sources.tn_publish import (
    TnPublishError,
    TnPublishSettings,
    generate_catalog_tn_thumbnail,
    render_destination,
)

DEFAULT_TITLES = (
    "During A Satsang At The Mystic Musings Program Sadhguru Spoke About Solar Flares",
    "Meera Bais Bhakti Was Sweet And Crazy",
)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--title",
        action="append",
        dest="titles",
        help="Catalog title substring to match (repeatable)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-render even if output file already exists",
    )
    args = parser.parse_args()
    needles = args.titles or list(DEFAULT_TITLES)

    settings = load_settings(PROJECT_ROOT)
    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    drive = GoogleDriveClient.from_service_account(
        PROJECT_ROOT / settings.google_sheets_service_account
    )
    tn_settings = TnPublishSettings(
        original_dir=PROJECT_ROOT / "downloads" / "original-thumbnails",
        cache_dir=PROJECT_ROOT / "downloads" / "tn-cache",
        output_dir=PROJECT_ROOT / "downloads" / "tn-rendered",
        english_override_file=PROJECT_ROOT / "downloads" / "tn-english-overrides.json",
    )
    tn_settings.output_dir.mkdir(parents=True, exist_ok=True)
    tn_settings.cache_dir.mkdir(parents=True, exist_ok=True)
    tn_settings.original_dir.mkdir(parents=True, exist_ok=True)

    records = airtable.list_records()
    matched = [
        record
        for record in records
        if any(needle.casefold() in catalog_title(record.fields).casefold() for needle in needles)
    ]

    if not matched:
        print("No matching records found.")
        return 1

    ok = 0
    failures: list[tuple[str, str]] = []
    for record in sorted(matched, key=lambda item: catalog_title(item.fields).casefold()):
        title = catalog_title(record.fields)
        destination = render_destination(tn_settings.output_dir, title)
        if destination.is_file() and args.force:
            destination.unlink()

        print(title)
        try:
            path = generate_catalog_tn_thumbnail(
                title=title,
                record_fields=record.fields,
                drive=drive,
                settings=tn_settings,
            )
            ok += 1
            size_kb = path.stat().st_size // 1024
            print(f"  OK {path.name} ({size_kb} KB)")
        except TnPublishError as exc:
            failures.append((title, str(exc)))
            print(f"  FAIL {exc}")
        print()

    print(f"Rendered: {ok}/{len(matched)}")
    if failures:
        for title, reason in failures:
            print(f"  - {title}: {reason}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
