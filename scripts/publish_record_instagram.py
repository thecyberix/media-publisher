"""Publish one catalog record to Instagram, downloading media from Drive when needed."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import requests

from catalog_parser.drive_docs import extract_drive_folder_id
from catalog_parser.ffmpeg_bundle import ensure_ffmpeg_bundled
from media_publisher.__main__ import (
    PROJECT_ROOT,
    meta_client_from_settings,
    meta_settings_missing,
    resolve_meta_targets,
    template_urls_from_settings,
)
from media_publisher.config import load_settings
from media_publisher.publishers.instagram import InstagramPublishError, publish_to_instagram
from media_publisher.publishers.meta import MetaError
from media_publisher.sources.airtable import (
    AirtableClient,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_VIDEO_FOLDER,
    mark_platform_scheduled,
    record_to_publish_job,
)
from media_publisher.sources.google_drive import GoogleDriveClient

REF_VIDEO_RE = re.compile(r"^REF_.+\.(mp4|mov|m4v)$", re.IGNORECASE)
DOWNLOAD_DIR = PROJECT_ROOT / "downloads" / "ig-publish"


def _sanitize_filename(name: str) -> str:
    cleaned = "".join("_" if ch in '<>:"/\\|?*' or ord(ch) < 32 else ch for ch in name)
    cleaned = cleaned.rstrip(" .")
    return cleaned or "video.mp4"


def _download_ref_video(drive: GoogleDriveClient, folder_link: str, dest_dir: Path) -> Path:
    folder_id = extract_drive_folder_id(folder_link)
    if not folder_id:
        raise RuntimeError(f"Could not parse Drive folder id from {folder_link!r}")
    ref_files = [
        item
        for item in drive.list_children(folder_id)
        if REF_VIDEO_RE.match(item.name) and item.mime_type.startswith("video/")
    ]
    if not ref_files:
        raise RuntimeError(f"No REF_ video found in Drive folder {folder_id}")
    ref = ref_files[0]
    dest = dest_dir / _sanitize_filename(ref.name)
    if dest.exists() and dest.stat().st_size > 0:
        print(f"Using cached video: {dest}", flush=True)
        return dest
    print(f"Downloading {ref.name} from Drive...", flush=True)
    return drive.download_file(ref.id, dest)


def _download_thumbnail(fields: dict, dest_dir: Path) -> Path | None:
    attachments = fields.get(FIELD_ORIGINAL_VIDEO_THUMBNAIL)
    if not isinstance(attachments, list) or not attachments:
        return None
    first = attachments[0]
    if not isinstance(first, dict):
        return None
    url = first.get("url")
    filename = first.get("filename") or "cover.jpg"
    if not isinstance(url, str) or not url.strip():
        return None
    dest = dest_dir / _sanitize_filename(str(filename))
    if dest.exists() and dest.stat().st_size > 0:
        print(f"Using cached thumbnail: {dest}", flush=True)
        return dest
    print(f"Downloading thumbnail {filename}...", flush=True)
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record_id", help="Airtable record id")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Download media and print paths without publishing",
    )
    parser.add_argument(
        "--no-cover",
        action="store_true",
        help="Publish without a Reel cover image",
    )
    args = parser.parse_args()

    settings = load_settings(PROJECT_ROOT)
    missing = meta_settings_missing(settings)
    if missing and not args.dry_run:
        print("Missing required settings:", ", ".join(missing))
        return 1

    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    record = airtable.get_record(args.record_id)
    title = record.fields.get("Title") or record.id
    print(f"Record: {title}", flush=True)

    folder_link = record.fields.get(FIELD_VIDEO_FOLDER)
    if not isinstance(folder_link, str) or not folder_link.strip():
        print(f"Missing {FIELD_VIDEO_FOLDER!r} on record")
        return 1

    service_account = PROJECT_ROOT / settings.google_sheets_service_account
    if not service_account.exists():
        print(f"Google service account not found: {service_account}")
        return 1

    dest_dir = DOWNLOAD_DIR / args.record_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    drive = GoogleDriveClient.from_service_account(service_account)
    video_path = _download_ref_video(drive, folder_link, dest_dir)
    size_mb = video_path.stat().st_size / (1024 * 1024)
    print(f"Video: {video_path} ({size_mb:.1f} MB)", flush=True)

    thumbnail_path = _download_thumbnail(record.fields, dest_dir)
    if thumbnail_path:
        print(f"Thumbnail: {thumbnail_path}", flush=True)
    else:
        print("No Original Video Thumbnail on record; publishing without cover.", flush=True)

    job = record_to_publish_job(record)
    job.video_path = str(video_path)
    job.video_url = None
    if thumbnail_path and not args.no_cover:
        job.thumbnail_path = str(thumbnail_path)
    elif args.no_cover:
        print("Skipping cover image.", flush=True)

    if args.dry_run:
        print("Dry run complete.")
        return 0

    if not settings.meta_app_id:
        print("Missing META_APP_ID — required for local video upload to Instagram.")
        return 1

    ffmpeg_path = settings.happyscribe_ffmpeg
    if not ffmpeg_path:
        ffmpeg_path = str(ensure_ffmpeg_bundled().ffmpeg_path)
        print(f"Using bundled ffmpeg: {ffmpeg_path}", flush=True)

    try:
        page_id, instagram_account_id, page_info = resolve_meta_targets(settings)
        meta_client = meta_client_from_settings(settings)
        media_id = publish_to_instagram(
            job,
            instagram_account_id=instagram_account_id,
            access_token=settings.meta_access_token or "",
            app_id=settings.meta_app_id,
            page_id=page_id,
            ffmpeg_path=ffmpeg_path,
            **template_urls_from_settings(settings),
        )
        permalink = meta_client.get_instagram_media_permalink(media_id)
        mark_platform_scheduled(
            airtable,
            record_id=record.id,
            record_fields=dict(record.fields),
            platform="instagram",
            permalink=permalink,
        )
    except (InstagramPublishError, MetaError) as exc:
        print(f"Instagram publish failed: {exc}", flush=True)
        return 1

    ig_handle = page_info.instagram_username or settings.meta_instagram_username
    print(f"Published to @{ig_handle}: {permalink}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
