"""Report how many catalog videos have a root-level image in their Drive folder."""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_STATUS,
    FIELD_TYPE,
    FIELD_VIDEO_FOLDER,
    TYPE_QUOTE,
    AirtableClient,
    catalog_title,
)
from media_publisher.sources.google_drive import GoogleDriveClient

STATUS_KEYS = (
    "To do",
    "Translation done",
    "Editing done",
    "Synchronization done",
)

FOLDER_ID_RE = re.compile(r"(?:folders/|folder/)([a-zA-Z0-9_-]+)")
IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".psd",
}


def parse_folder_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    match = FOLDER_ID_RE.search(text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{10,}", text):
        return text
    return None


def status_bucket(status: object) -> str | None:
    if status is None:
        return None
    text = str(status)
    for key in STATUS_KEYS:
        if key.casefold() in text.casefold():
            return key
    return None


def is_image_file(name: str, mime_type: str) -> bool:
    if mime_type.startswith("image/"):
        return True
    if "photoshop" in mime_type.casefold():
        return True
    return Path(name).suffix.casefold() in IMAGE_EXTENSIONS


def build_filter_formula() -> str:
    clauses = [f'FIND("{key}", {{Status}} & "")' for key in STATUS_KEYS]
    type_clause = f'{{Type}} != "{TYPE_QUOTE}"'
    return f"AND(OR({', '.join(clauses)}), {type_clause})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--show-missing",
        action="store_true",
        help="List videos without a root-level image file",
    )
    args = parser.parse_args()

    settings = load_settings(PROJECT_ROOT)
    sa_path = PROJECT_ROOT / "credentials" / "google-sheets-service-account.json"

    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    drive = GoogleDriveClient.from_service_account(sa_path)

    records = airtable.list_records(
        filter_formula=build_filter_formula(),
    )

    folder_cache: dict[str, list] = {}
    summary: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "total": 0,
            "with_folder": 0,
            "with_image": 0,
            "no_folder": 0,
            "no_image": 0,
            "drive_error": 0,
        }
    )
    missing: dict[str, list[tuple[str, str]]] = defaultdict(list)

    for record in records:
        fields = record.fields
        bucket = status_bucket(fields.get(FIELD_STATUS))
        if bucket is None:
            continue

        title = catalog_title(fields)
        summary[bucket]["total"] += 1

        folder_id = parse_folder_id(fields.get(FIELD_VIDEO_FOLDER))
        if folder_id is None:
            summary[bucket]["no_folder"] += 1
            missing[bucket].append((title, "no Video Folder link"))
            continue

        summary[bucket]["with_folder"] += 1
        try:
            if folder_id not in folder_cache:
                children = drive.list_children(folder_id)
                images = [
                    child
                    for child in children
                    if is_image_file(child.name, child.mime_type)
                ]
                folder_cache[folder_id] = images
            images = folder_cache[folder_id]
        except Exception as exc:
            summary[bucket]["drive_error"] += 1
            missing[bucket].append((title, f"drive error: {exc}"))
            continue

        if images:
            summary[bucket]["with_image"] += 1
        else:
            summary[bucket]["no_image"] += 1
            missing[bucket].append((title, "no root-level image"))

    print("=== Video folder image audit (root-level image files) ===")
    print(f"Total matching records: {len(records)}")
    print()

    for key in STATUS_KEYS:
        stats = summary[key]
        if stats["total"] == 0:
            print(f"{key}: 0 videos")
            continue

        pct = 100.0 * stats["with_image"] / stats["total"]
        print(f"{key}:")
        print(f"  total videos:       {stats['total']}")
        print(f"  with folder link:   {stats['with_folder']}")
        print(f"  with root image:    {stats['with_image']} ({pct:.0f}%)")
        print(f"  no folder link:     {stats['no_folder']}")
        print(f"  folder, no image:   {stats['no_image']}")
        print(f"  drive errors:       {stats['drive_error']}")
        print()

    if args.show_missing:
        for key in STATUS_KEYS:
            items = missing[key]
            if not items:
                continue
            print(f"--- {key}: {len(items)} without usable root image ---")
            for title, reason in items:
                print(f"  - {title} ({reason})")
            print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
