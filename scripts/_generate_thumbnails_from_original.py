"""Generate TN thumbnails from Airtable Original Video Thumbnail + translated caption.

Uses the original English thumbnail for line position and style. When a matching
image exists in the Video Folder, that Drive file is the background (PSD text
layers are stripped; raster files have detected English covered).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_VIDEO_CAPTION_TRANSLATED,
    FIELD_VIDEO_FOLDER,
    AirtableClient,
    catalog_title,
)
from media_publisher.sources.google_drive import GoogleDriveClient, GoogleDriveError
from media_publisher.sources.tn_docx import caption_lines_for_render
from media_publisher.sources.tn_psd import ImageSize
from media_publisher.sources.tn_publish import (
    DriveTnTemplate,
    compose_offline_tn_background,
    load_matching_drive_tn_template,
    parse_folder_id,
    render_destination,
)
from media_publisher.sources.tn_reference import (
    cover_reference_layout,
    derive_reference_layout,
)
from media_publisher.sources.tn_renderer import TnRenderError, render_tn_thumbnail

DEFAULT_TITLES = (
    "During A Satsang At The Mystic Musings Program Sadhguru Spoke About Solar Flares",
    "Meera Bais Bhakti Was Sweet And Crazy",
)


def download_airtable_thumbnail(fields: dict, destination: Path) -> None:
    attachment = fields.get(FIELD_ORIGINAL_VIDEO_THUMBNAIL)
    if not isinstance(attachment, list) or not attachment:
        raise RuntimeError("Original Video Thumbnail is missing in Airtable")
    first = attachment[0]
    if not isinstance(first, dict):
        raise RuntimeError("Original Video Thumbnail attachment is invalid")
    url = str(first.get("url") or "").strip()
    if not url:
        raise RuntimeError("Original Video Thumbnail attachment has no URL")
    destination.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    destination.write_bytes(response.content)


def load_drive_background(
    drive: GoogleDriveClient | None,
    fields: dict,
    *,
    cache_dir: Path,
    reference_size: ImageSize,
) -> DriveTnTemplate | None:
    if drive is None:
        return None
    folder_id = parse_folder_id(fields.get(FIELD_VIDEO_FOLDER))
    if folder_id is None:
        return None
    try:
        return load_matching_drive_tn_template(
            drive,
            folder_id,
            cache_dir=cache_dir,
            reference_size=reference_size,
        )
    except (GoogleDriveError, OSError) as exc:
        print(f"  Drive template skipped: {exc}")
        return None


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", action="append", dest="titles")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    needles = args.titles or list(DEFAULT_TITLES)

    settings = load_settings(PROJECT_ROOT)
    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    output_dir = PROJECT_ROOT / "downloads" / "tn-rendered"
    staging_dir = PROJECT_ROOT / "downloads" / "original-thumbnails"
    cache_dir = PROJECT_ROOT / "downloads" / "tn-cache"
    output_dir.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    drive: GoogleDriveClient | None = None
    service_account = PROJECT_ROOT / settings.google_sheets_service_account
    if service_account.is_file():
        try:
            drive = GoogleDriveClient.from_service_account(service_account)
        except GoogleDriveError as exc:
            print(f"Drive client unavailable ({exc}); using original thumbnail as background.")
    else:
        print("Drive service account missing; using original thumbnail as background.")

    records = [
        record
        for record in airtable.list_records()
        if any(
            needle.casefold() in catalog_title(record.fields).casefold()
            for needle in needles
        )
    ]
    if not records:
        print("No matching records found.")
        return 1

    ok = 0
    failures: list[tuple[str, str]] = []
    for record in sorted(records, key=lambda item: catalog_title(item.fields).casefold()):
        fields = record.fields
        title = catalog_title(fields)
        destination = render_destination(output_dir, title)
        if destination.is_file() and args.force:
            destination.unlink()

        print(title)
        caption = fields.get(FIELD_VIDEO_CAPTION_TRANSLATED)
        if not isinstance(caption, str) or not caption.strip():
            failures.append((title, "missing Video caption translated in Airtable"))
            print("  FAIL missing translated caption")
            print()
            continue

        caption_lines = caption_lines_for_render(caption.strip())
        if not caption_lines:
            failures.append((title, "could not parse translated caption"))
            print("  FAIL could not parse translated caption")
            print()
            continue

        staging_path = staging_dir / f"{destination.stem}.original.jpg"
        try:
            download_airtable_thumbnail(fields, staging_path)
            with Image.open(staging_path) as image:
                original = image.convert("RGB")
        except (RuntimeError, OSError, requests.RequestException) as exc:
            failures.append((title, str(exc)))
            print(f"  FAIL {exc}")
            print()
            continue

        caption_text = "\n".join(caption_lines)
        print(f"  original: {original.size[0]}x{original.size[1]}")
        print(f"  caption:  {' / '.join(caption_lines)}")

        derived = derive_reference_layout(
            original,
            caption_line_count=len(caption_lines),
            catalog_title=title,
        )
        if derived is None:
            failures.append((title, "could not derive line styles from original English text"))
            print("  FAIL could not derive line styles from original English text")
            print()
            continue
        line_styles, cover_mode = derived
        covered_original = cover_reference_layout(
            original,
            cover_mode,
            line_styles,
            len(caption_lines),
        )

        drive_template = load_drive_background(
            drive,
            fields,
            cache_dir=cache_dir,
            reference_size=ImageSize(
                width=original.size[0],
                height=original.size[1],
                source="original-thumbnail",
            ),
        )
        template, line_styles, background_source = compose_offline_tn_background(
            original=original,
            covered_original=covered_original,
            drive_template=drive_template,
            cover_mode=cover_mode,
            line_styles=line_styles,
            caption_line_count=len(caption_lines),
        )
        print(
            f"  styles:   {len(line_styles)} reference line(s) "
            f"from original English layout ({cover_mode})"
        )
        print(
            f"  background: {background_source} "
            f"{template.size[0]}x{template.size[1]}"
        )

        try:
            result = render_tn_thumbnail(
                template=template,
                english_text=caption_text,
                line_styles=line_styles,
                destination=destination,
                catalog_title=title,
            )
        except TnRenderError as exc:
            failures.append((title, str(exc)))
            print(f"  FAIL {exc}")
            print()
            continue

        ok += 1
        print(
            f"  OK {result.width}x{result.height}, {result.line_count} line(s) -> {destination.name}"
        )
        print()

    print(f"Rendered: {ok}/{len(records)}")
    if failures:
        for title, reason in failures:
            print(f"  - {title}: {reason}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
