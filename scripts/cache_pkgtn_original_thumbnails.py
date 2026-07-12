"""Download original-platform thumbnails for pkgTn-marked unpublished videos.

These are the source YouTube/Instagram cover images from the Original Video link.
They match what Airtable's Original Video Thumbnail field uses when a Video Folder
has a root TN JPG/PSD/PDF marker file.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.cache_pkgtn_thumbnails import (
    build_filter_formula,
    fetch_catalog_records,
    index_catalog,
    match_catalog_row,
    status_bucket,
    tn_is_marked,
)
from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO,
    FIELD_STATUS,
    AirtableClient,
    catalog_title,
)
from media_publisher.sources.source_thumbnail import (
    SourceThumbnailError,
    fetch_original_video_thumbnail,
    original_thumbnail_destination,
)

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "downloads" / "original-thumbnails"


def main() -> int:
    settings = load_settings(PROJECT_ROOT)
    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    catalog = fetch_catalog_records()
    by_url, by_title = index_catalog(catalog)

    targets: list[dict[str, str]] = []
    for record in airtable.list_records(filter_formula=build_filter_formula()):
        fields = record.fields
        if status_bucket(fields.get(FIELD_STATUS)) is None:
            continue
        sheet_row = match_catalog_row(fields, by_url, by_title)
        if sheet_row is None or not tn_is_marked(sheet_row.get("pkgTn")):
            continue
        title = catalog_title(fields)
        source_url = str(fields.get(FIELD_ORIGINAL_VIDEO) or "").strip()
        targets.append(
            {
                "title": title,
                "status": str(status_bucket(fields.get(FIELD_STATUS)) or ""),
                "source_url": source_url,
            }
        )

    output_dir = DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    summary = Counter()
    failures: list[tuple[str, str]] = []

    print(
        f"=== Fetch original thumbnails for {len(targets)} pkgTn videos ===\n"
        f"Output: {output_dir}\n"
    )

    for item in sorted(targets, key=lambda row: (row["status"], row["title"])):
        title = item["title"]
        source_url = item["source_url"]
        destination = original_thumbnail_destination(output_dir, title)
        print(title)
        print(f"  source: {source_url or '(missing)'}")

        if not source_url:
            summary["failed"] += 1
            failures.append((title, "missing Original Video link in Airtable"))
            print("  FAIL missing Original Video link")
            print()
            continue

        try:
            result = fetch_original_video_thumbnail(source_url, destination)
        except SourceThumbnailError as exc:
            summary["failed"] += 1
            failures.append((title, str(exc)))
            print(f"  FAIL {exc}")
            print()
            continue

        summary["ok"] += 1
        print(f"  OK {result.width}x{result.height} via {result.method}")
        print(f"  -> {destination.name}")
        manifest.append(
            {
                "title": title,
                "status": item["status"],
                "source_url": source_url,
                "platform": result.platform,
                "method": result.method,
                "thumbnail_url": result.thumbnail_url,
                "airtable_file": str(destination),
            }
        )
        print()

    manifest_path = output_dir / "pkgtn-original-thumbnails-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("=== Summary ===")
    print(f"Downloaded: {summary['ok']}")
    print(f"Failed:     {summary['failed']}")
    print(f"Manifest:   {manifest_path}")
    if failures:
        print()
        print("Failures:")
        for title, reason in failures:
            print(f"  - {title}: {reason}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
