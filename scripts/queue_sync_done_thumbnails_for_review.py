"""Queue original-platform thumbnails for review (catalog status, no Airtable thumb).

For each matching catalog video:
1. Fetch the original-platform thumbnail from Original Video.
2. Keep it only when aspect ratio matches the Drive video reference.
3. Upload to the Drive review folder (not Airtable).
4. Send one review notification email for the whole run.

Also processes approved review thumbnails on every run.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_STATUS,
    AirtableClient,
    catalog_title,
    has_original_video_thumbnail,
)
from media_publisher.sources.google_drive import GoogleDriveClient
from media_publisher.sources.source_thumbnail import (
    SourceThumbnailError,
    fetch_original_video_thumbnail,
    original_thumbnail_destination,
)
from media_publisher.sources.thumbnail_review import (
    DEFAULT_REVIEW_FOLDER_URL,
    ReviewQueueItem,
    process_approved_review_thumbnails,
    review_drive_filename,
    send_review_notification_email,
    thumbnail_matches_reference_aspect,
    upload_review_thumbnail,
)
from media_publisher.sources.tn_publish import parse_folder_id, reference_thumbnail_size

MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def _configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


_configure_stdio()


def save_upload_jpeg(image: Image.Image, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    rgb = image.convert("RGB")
    for quality in (92, 85, 78, 70, 62):
        rgb.save(destination, format="JPEG", quality=quality, optimize=True)
        if destination.stat().st_size <= MAX_UPLOAD_BYTES:
            return destination

    scaled = rgb
    for max_side in (2400, 1920, 1600, 1280, 1080):
        width, height = scaled.size
        if max(width, height) <= max_side:
            break
        if width >= height:
            new_size = (max_side, max(1, round(height * max_side / width)))
        else:
            new_size = (max(1, round(width * max_side / height)), max_side)
        scaled = scaled.resize(new_size, Image.Resampling.LANCZOS)
        scaled.save(destination, format="JPEG", quality=78, optimize=True)
        if destination.stat().st_size <= MAX_UPLOAD_BYTES:
            return destination

    raise RuntimeError(
        f"Could not compress {destination.name} under Airtable 5 MB limit"
    )


def existing_review_names(drive: GoogleDriveClient, review_folder_id: str) -> set[str]:
    names: set[str] = set()
    for item in drive.list_children(review_folder_id):
        if item.mime_type.startswith("image/"):
            names.add(item.name.casefold())
    approved = drive.find_child_folder(review_folder_id, "Approved")
    if approved is not None:
        for item in drive.list_children(approved.id):
            if item.mime_type.startswith("image/"):
                names.add(item.name.casefold())
    return names


def status_matches_any(value: object, status_keys: list[str]) -> bool:
    if value is None:
        return False
    text = str(value).casefold()
    return any(key.casefold() in text for key in status_keys)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status",
        action="append",
        default=[],
        help='Catalog status bucket to process (repeatable; default: "Synchronization done")',
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Upload review thumbnails and process approved ones (default dry-run)",
    )
    args = parser.parse_args()
    status_keys = [part.strip() for item in args.status for part in item.split(",") if part.strip()]
    if not status_keys:
        status_keys = ["Synchronization done"]

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
    original_dir.mkdir(parents=True, exist_ok=True)
    export_dir = PROJECT_ROOT / "downloads" / "thumbnail-review-queue"
    export_dir.mkdir(parents=True, exist_ok=True)

    review_folder_id = settings.thumbnail_review_drive_folder_id
    approved_subfolder = settings.thumbnail_review_approved_subfolder

    records = airtable.list_records()
    approved_results = process_approved_review_thumbnails(
        airtable,
        drive,
        records,
        review_folder_id=review_folder_id,
        approved_subfolder=approved_subfolder,
        apply=args.apply,
    )

    pending_review_names = existing_review_names(drive, review_folder_id)
    review_queue: list[ReviewQueueItem] = []
    skipped: list[tuple[str, str]] = []

    for record in records:
        fields = record.fields
        if not status_matches_any(fields.get(FIELD_STATUS), status_keys):
            continue
        if has_original_video_thumbnail(fields):
            continue

        title = catalog_title(fields)
        source_url = str(fields.get(FIELD_ORIGINAL_VIDEO) or "").strip()
        if not source_url:
            skipped.append((title, "missing Original Video URL"))
            continue

        folder_id = parse_folder_id(fields.get("Video Folder"))
        if folder_id is None:
            skipped.append((title, "missing Video Folder"))
            continue

        review_name = review_drive_filename(title).casefold()
        if review_name in pending_review_names:
            skipped.append((title, "already in review queue"))
            continue

        reference = reference_thumbnail_size(
            fields,
            title=title,
            original_dir=original_dir,
            drive=drive,
            folder_id=folder_id,
        )
        if reference is None:
            skipped.append((title, "no aspect-ratio reference"))
            continue

        original_path = original_thumbnail_destination(original_dir, title)
        try:
            fetch_original_video_thumbnail(source_url, original_path)
        except SourceThumbnailError as exc:
            skipped.append((title, str(exc)))
            continue

        with Image.open(original_path) as image:
            original = image.convert("RGB")

        if not thumbnail_matches_reference_aspect(
            original,
            reference_width=reference.width,
            reference_height=reference.height,
        ):
            skipped.append(
                (
                    title,
                    "aspect ratio mismatch "
                    f"({original.size[0]}x{original.size[1]} vs "
                    f"{reference.width}x{reference.height})",
                )
            )
            continue

        export_path = export_dir / review_drive_filename(title)
        try:
            save_upload_jpeg(original, export_path)
        except RuntimeError as exc:
            skipped.append((title, str(exc)))
            continue

        review_queue.append(
            ReviewQueueItem(
                record_id=record.id,
                title=title,
                local_path=export_path,
                reason="original-platform thumbnail fallback (matching aspect ratio)",
            )
        )

    mode = "APPLY" if args.apply else "DRY-RUN"
    status_label = ", ".join(status_keys)
    print(f"=== Queue {status_label} thumbnails for review ({mode}) ===")
    print(f"Review folder: {DEFAULT_REVIEW_FOLDER_URL}\n")

    if approved_results:
        print(f"--- Approved review thumbnails ({len(approved_results)}) ---")
        for item in sorted(approved_results, key=lambda row: row.title.casefold()):
            print(f"- {item.title} ({item.action})")
        print()

    if review_queue:
        print(f"--- Review queue ({len(review_queue)}) ---")
        for item in sorted(review_queue, key=lambda row: row.title.casefold()):
            size_kb = item.local_path.stat().st_size // 1024
            print(f"PLAN {item.title}")
            print(f"     file: {item.local_path.name} ({size_kb} KB)")

    if skipped:
        print()
        print(f"--- Skipped ({len(skipped)}) ---")
        for title, reason in sorted(skipped, key=lambda item: item[0].casefold()):
            print(f"- {title}: {reason}")

    print()
    print("=== Summary ===")
    print(f"Review queue: {len(review_queue)}")
    print(f"Skipped:      {len(skipped)}")
    print(f"Approved:     {len(approved_results)}")

    if not args.apply:
        print()
        print("Dry-run only. Re-run with --apply to upload to Drive and send email.")
        return 0

    review_uploaded_items: list[ReviewQueueItem] = []
    failures: list[tuple[str, str]] = []
    for item in review_queue:
        try:
            upload_review_thumbnail(
                drive.drive_service,
                review_folder_id=review_folder_id,
                local_path=item.local_path,
                title=item.title,
            )
            review_uploaded_items.append(item)
            print(f"REVIEW {item.title}")
        except Exception as exc:
            failures.append((item.title, str(exc)))
            print(f"FAIL REVIEW {item.title}")
            print(f"     {exc}")

    if review_uploaded_items and send_review_notification_email(
        review_uploaded_items,
        review_folder_url=DEFAULT_REVIEW_FOLDER_URL,
    ):
        print(f"EMAIL review notification sent ({len(review_uploaded_items)} video(s))")
    elif review_uploaded_items:
        print("WARN review uploads succeeded but notification email was not sent")

    if failures:
        print()
        print("Failures:")
        for title, reason in failures:
            print(f"  - {title}: {reason}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
