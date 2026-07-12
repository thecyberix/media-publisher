"""Download original-platform thumbnails for unpublished catalog videos."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


_configure_stdio()

from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_STATUS,
    FIELD_TYPE,
    TYPE_QUOTE,
    AirtableClient,
    catalog_title,
    has_original_video_thumbnail,
)
from media_publisher.sources.airtable_thumbnail import resolve_original_platform_attachment
from media_publisher.sources.source_thumbnail import (
    SourceThumbnailError,
    fetch_original_video_thumbnail,
    original_thumbnail_destination,
)

STATUS_KEYS = (
    "To do",
    "Translation done",
    "Editing done",
    "Synchronization done",
)
DEFAULT_DOWNLOAD_DIR = PROJECT_ROOT / "downloads" / "original-thumbnails"


def status_bucket(status: object) -> str | None:
    if status is None:
        return None
    text = str(status)
    for key in STATUS_KEYS:
        if key.casefold() in text.casefold():
            return key
    return None


def build_filter_formula() -> str:
    clauses = [f'FIND("{key}", {{Status}} & "")' for key in STATUS_KEYS]
    type_clause = f'{{Type}} != "{TYPE_QUOTE}"'
    return f"AND(OR({', '.join(clauses)}), {type_clause})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--download-dir",
        type=Path,
        default=DEFAULT_DOWNLOAD_DIR,
        help="Directory for downloaded original thumbnails",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download and replace existing local/Airtable thumbnails",
    )
    parser.add_argument(
        "--apply-airtable",
        action="store_true",
        help="Attach fetched thumbnails to Airtable Original Video Thumbnail",
    )
    args = parser.parse_args()

    settings = load_settings(PROJECT_ROOT)
    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    records = airtable.list_records(filter_formula=build_filter_formula())

    targets: list[dict[str, str]] = []
    for record in records:
        fields = record.fields
        bucket = status_bucket(fields.get(FIELD_STATUS))
        if bucket is None:
            continue
        title = catalog_title(fields)
        original_video = str(fields.get(FIELD_ORIGINAL_VIDEO) or "").strip()
        targets.append(
            {
                "record_id": record.id,
                "title": title,
                "type": str(fields.get(FIELD_TYPE) or ""),
                "status": bucket,
                "original_video": original_video,
                "has_airtable_thumb": has_original_video_thumbnail(fields),
            }
        )

    args.download_dir.mkdir(parents=True, exist_ok=True)
    print(f"=== Fetch original thumbnails ({len(targets)} videos) ===")
    print(f"Download dir: {args.download_dir.resolve()}")
    if args.apply_airtable:
        print("Airtable:     will update Original Video Thumbnail")
    print()

    ok = 0
    skipped = 0
    skipped_airtable = 0
    failed: list[tuple[str, str]] = []
    by_platform: dict[str, int] = defaultdict(int)
    by_status: dict[str, Counter] = defaultdict(Counter)
    manifest: list[dict] = []

    for item in sorted(targets, key=lambda row: (row["status"], row["title"])):
        title = item["title"]
        source_url = item["original_video"]
        destination = original_thumbnail_destination(args.download_dir, title)
        status = item["status"]

        print(f"{title}")
        print(f"  status: {status} | type: {item['type']}")
        print(f"  source: {source_url or '(missing)'}")

        if not source_url:
            failed.append((title, "missing Original Video link in Airtable"))
            by_status[status]["failed"] += 1
            print("  result: FAILED (no link)")
            print()
            continue

        local_exists = destination.exists()
        skip_local = local_exists and not args.force
        skip_airtable = item["has_airtable_thumb"] and not args.force
        if skip_local and (not args.apply_airtable or skip_airtable):
            skipped += 1
            by_status[status]["skipped"] += 1
            print(f"  result: SKIPPED (exists: {destination.name})")
            print()
            continue

        try:
            if skip_local:
                print(f"  local:  SKIPPED (exists: {destination.name})")
                attachment, method = resolve_original_platform_attachment(source_url)
                platform = attachment[0]["filename"].removeprefix("original-").removesuffix(".jpg")
            else:
                result = fetch_original_video_thumbnail(source_url, destination)
                attachment, method = resolve_original_platform_attachment(source_url)
                platform = result.platform
                by_platform[platform] += 1
                print(f"  result: OK {result.width}x{result.height} via {result.method}")
                print(f"  saved:  {destination.name}")
        except SourceThumbnailError as exc:
            failed.append((title, str(exc)))
            by_status[status]["failed"] += 1
            print(f"  result: FAILED ({exc})")
            print()
            continue

        if args.apply_airtable:
            if skip_airtable:
                skipped_airtable += 1
                print("  airtable: SKIPPED (already set)")
            else:
                try:
                    airtable.update_record(
                        item["record_id"],
                        {FIELD_ORIGINAL_VIDEO_THUMBNAIL: attachment},
                    )
                    print(f"  airtable: OK ({method})")
                except Exception as exc:
                    failed.append((title, f"Airtable update failed: {exc}"))
                    by_status[status]["failed"] += 1
                    print(f"  airtable: FAILED ({exc})")
                    print()
                    continue

        ok += 1
        by_status[status]["ok"] += 1
        manifest.append(
            {
                "title": title,
                "status": status,
                "source_url": source_url,
                "platform": platform,
                "method": method,
                "local_file": str(destination),
            }
        )
        print()

    manifest_path = args.download_dir / "original-thumbnails-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("=== Summary ===")
    print(f"Downloaded/updated: {ok}")
    print(f"Skipped:            {skipped}")
    if args.apply_airtable:
        print(f"Airtable skipped:   {skipped_airtable}")
    print(f"Failed:             {len(failed)}")
    if by_platform:
        print(
            "Platforms:          "
            + ", ".join(f"{name}={count}" for name, count in sorted(by_platform.items()))
        )
    print()
    for key in STATUS_KEYS:
        stats = by_status[key]
        if not stats:
            continue
        print(
            f"{key}: ok={stats.get('ok', 0)} "
            f"skipped={stats.get('skipped', 0)} failed={stats.get('failed', 0)}"
        )
    print(f"\nManifest: {manifest_path}")
    if failed:
        print()
        print("Failures:")
        for title, reason in failed:
            print(f"  - {title}: {reason}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
