"""Process approved review thumbnails: upload to Airtable and delete from Drive."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from media_publisher.config import load_settings
from media_publisher.sources.airtable import AirtableClient
from media_publisher.sources.drive_layout import resolve_thumbnails_for_approval_id
from media_publisher.sources.google_drive import GoogleDriveClient
from media_publisher.sources.thumbnail_review import process_approved_review_thumbnails


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    settings = load_settings(PROJECT_ROOT)
    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    drive = GoogleDriveClient.from_service_account(
        PROJECT_ROOT / settings.google_sheets_service_account
    )
    review_folder_id = resolve_thumbnails_for_approval_id(
        drive,
        drive_url=settings.drive_url,
    )

    records = airtable.list_records()
    results = process_approved_review_thumbnails(
        airtable,
        drive,
        records,
        review_folder_id=review_folder_id,
        approved_subfolder=settings.thumbnail_review_approved_subfolder,
        apply=True,
        project_root=PROJECT_ROOT,
    )

    print("=== Approved thumbnail check ===")
    if not results:
        print("No approved thumbnails found in Drive Approved subfolder.")
        return 0

    for item in sorted(results, key=lambda row: row.title.casefold()):
        caption = item.caption_action
        if item.caption_detail:
            caption = f"{item.caption_action} ({item.caption_detail})"
        print(f"OK   {item.title} ({item.action})")
        print(f"     drive file: {item.drive_file}")
        print(f"     caption: {caption}")

    print()
    print(f"Processed: {len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
