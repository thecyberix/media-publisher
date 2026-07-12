"""One-off: list videos with root images and their Type."""
from __future__ import annotations

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
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".psd",
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
    settings = load_settings(PROJECT_ROOT)
    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    drive = GoogleDriveClient.from_service_account(
        PROJECT_ROOT / "credentials" / "google-sheets-service-account.json"
    )
    records = airtable.list_records(filter_formula=build_filter_formula())

    folder_cache: dict[str, list] = {}
    with_image: list[dict] = []

    for record in records:
        fields = record.fields
        bucket = status_bucket(fields.get(FIELD_STATUS))
        if bucket is None:
            continue
        folder_id = parse_folder_id(fields.get(FIELD_VIDEO_FOLDER))
        if folder_id is None:
            continue
        if folder_id not in folder_cache:
            children = drive.list_children(folder_id)
            folder_cache[folder_id] = [
                c for c in children if is_image_file(c.name, c.mime_type)
            ]
        images = folder_cache[folder_id]
        if not images:
            continue
        with_image.append(
            {
                "title": catalog_title(fields),
                "status": bucket,
                "type": fields.get(FIELD_TYPE) or "(none)",
                "images": [c.name for c in images],
            }
        )

    print(f"Videos with root image: {len(with_image)}\n")
    by_type: dict[str, list] = defaultdict(list)
    for item in with_image:
        by_type[str(item["type"])].append(item)

    for video_type, items in sorted(by_type.items(), key=lambda x: (-len(x[1]), x[0])):
        print(f"=== Type: {video_type} ({len(items)}) ===")
        for item in sorted(items, key=lambda x: (x["status"], x["title"])):
            names = ", ".join(item["images"][:3])
            if len(item["images"]) > 3:
                names += f" (+{len(item['images']) - 3} more)"
            print(f"  [{item['status']}] {item['title']}")
            print(f"    files: {names}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
