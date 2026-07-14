"""Probe Ducati/Sandeep thumbnail state."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

spec = importlib.util.spec_from_file_location(
    "audit_tn", PROJECT_ROOT / "scripts" / "audit_tn_and_canva.py"
)
audit = importlib.util.module_from_spec(spec)
sys.modules["audit_tn"] = audit
spec.loader.exec_module(audit)

spec_cache = importlib.util.spec_from_file_location(
    "cache_pkgtn", PROJECT_ROOT / "scripts" / "cache_pkgtn_thumbnails.py"
)
cache_pkgtn = importlib.util.module_from_spec(spec_cache)
spec_cache.loader.exec_module(cache_pkgtn)

from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    AirtableClient,
    catalog_title,
)
from media_publisher.sources.airtable_thumbnail import pick_root_thumbnail_marker
from media_publisher.sources.google_drive import GoogleDriveClient
from media_publisher.sources.source_thumbnail import (
    fetch_original_video_thumbnail,
    original_thumbnail_destination,
)
from media_publisher.sources.tn_psd import safe_cache_name
from media_publisher.sources.tn_publish import reference_thumbnail_size


def overall_similarity(left: Image.Image, right: Image.Image) -> float:
    if left.size != right.size:
        right = right.resize(left.size, Image.Resampling.LANCZOS)
    pixels_a = list(left.convert("RGB").getdata())
    pixels_b = list(right.convert("RGB").getdata())
    total = 0
    for (r1, g1, b1), (r2, g2, b2) in zip(pixels_a, pixels_b, strict=False):
        total += abs(r1 - r2) + abs(g1 - g2) + abs(b1 - b2)
    return 1.0 - total / (len(pixels_a) * 3 * 255)


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
    original_dir = PROJECT_ROOT / settings.tn_original_thumbnail_dir

    record = None
    for item in airtable.list_records(filter_formula=audit.build_filter_formula()):
        if "Ducati" in catalog_title(item.fields):
            record = item
            break
    if record is None:
        print("Record not found")
        return 1

    title = catalog_title(record.fields)
    attachment = record.fields.get(FIELD_ORIGINAL_VIDEO_THUMBNAIL) or []
    print(f"Title: {title}")
    if attachment:
        first = attachment[0]
        print(
            "Airtable attachment:",
            first.get("filename"),
            f"{first.get('width')}x{first.get('height')}",
        )
    else:
        print("Airtable attachment: (empty)")

    folder_id = audit.parse_folder_id(record.fields.get("Video Folder"))
    marker = pick_root_thumbnail_marker(drive.list_children(folder_id))
    print(f"Drive marker: {marker.name}")
    print(f"Drive mime: {marker.mime_type}")

    source_url = str(record.fields.get(FIELD_ORIGINAL_VIDEO) or "").strip()
    reference = reference_thumbnail_size(
        record.fields,
        title=title,
        original_dir=original_dir,
        drive=drive,
        folder_id=folder_id,
    )
    print(f"Reference size: {reference}")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / safe_cache_name(marker.name)
        drive.download_file(marker.id, path)
        drive_image = cache_pkgtn.flatten_drive_file(path, mime_type=marker.mime_type)
        print(f"Drive TN flattened: {drive_image.size[0]}x{drive_image.size[1]}")

        original_path = original_thumbnail_destination(original_dir, title)
        fetch_original_video_thumbnail(source_url, original_path)
        with Image.open(original_path) as image:
            original = image.convert("RGB")
        print(f"Original platform: {original.size[0]}x{original.size[1]}")
        print(f"Overall similarity: {overall_similarity(original, drive_image):.3f}")

    # PDFs are always treated as having baked text in the upload script.
    print("Upload rule applied: upload-drive-tn (PDF with baked English text)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
