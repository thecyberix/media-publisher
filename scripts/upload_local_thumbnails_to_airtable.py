"""Upload cached original-platform thumbnails from disk to Airtable.

Reads JPEGs from downloads/original-thumbnails and uploads them directly
via Airtable's uploadAttachment API (base64, up to 5 MB each).
"""
from __future__ import annotations

import argparse
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
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_STATUS,
    AirtableClient,
    catalog_title,
)
from media_publisher.sources.source_thumbnail import original_thumbnail_destination

DEFAULT_SOURCE_DIR = PROJECT_ROOT / "downloads" / "original-thumbnails"
SUFFIX = ".original-thumb.jpg"


def _configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


_configure_stdio()


def resolve_local_thumbnail(source_dir: Path, title: str) -> Path | None:
    expected = original_thumbnail_destination(source_dir, title)
    if expected.is_file():
        return expected

    stem = expected.name[: -len(SUFFIX)] if expected.name.endswith(SUFFIX) else expected.stem
    for candidate in source_dir.glob(f"*{SUFFIX}"):
        if candidate.stem == stem:
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help="Directory containing *.original-thumb.jpg files",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Upload to Airtable (default is dry-run)",
    )
    parser.add_argument(
        "--pkgtn-only",
        action="store_true",
        help="Only upload videos marked with pkgTn in the SM catalog",
    )
    args = parser.parse_args()

    source_dir = args.source_dir
    if not source_dir.is_dir():
        print(f"Source directory not found: {source_dir}")
        return 1

    settings = load_settings(PROJECT_ROOT)
    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )

    catalog_by_url = catalog_by_title = None
    if args.pkgtn_only:
        catalog = fetch_catalog_records()
        catalog_by_url, catalog_by_title = index_catalog(catalog)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Upload local thumbnails to Airtable ({mode}) ===")
    print(f"Source: {source_dir}\n")

    summary = Counter()
    failures: list[tuple[str, str]] = []
    planned: list[tuple[str, str, Path]] = []

    for record in airtable.list_records(filter_formula=build_filter_formula()):
        fields = record.fields
        if status_bucket(fields.get(FIELD_STATUS)) is None:
            continue

        if args.pkgtn_only:
            sheet_row = match_catalog_row(fields, catalog_by_url, catalog_by_title)
            if sheet_row is None or not tn_is_marked(sheet_row.get("pkgTn")):
                continue

        title = catalog_title(fields)
        local_file = resolve_local_thumbnail(source_dir, title)
        if local_file is None:
            summary["missing_file"] += 1
            failures.append((title, "no matching local thumbnail file"))
            print(f"MISS {title}")
            print(f"     expected: {original_thumbnail_destination(source_dir, title).name}")
            continue

        summary["found"] += 1
        planned.append((record.id, title, local_file))
        size_kb = local_file.stat().st_size // 1024
        print(f"PLAN {title}")
        print(f"     file: {local_file.name} ({size_kb} KB)")

    print()
    print("=== Summary ===")
    print(f"Matched local files: {summary['found']}")
    print(f"Missing local files:   {summary['missing_file']}")
    print(f"Planned uploads:       {len(planned)}")

    if not args.apply:
        print()
        print("Dry-run only. Re-run with --apply to upload to Airtable.")
        return 1 if failures else 0

    applied = 0
    for record_id, title, local_file in planned:
        try:
            airtable.upload_attachment(
                record_id,
                FIELD_ORIGINAL_VIDEO_THUMBNAIL,
                local_file,
            )
            applied += 1
            print(f"OK   {title}")
        except Exception as exc:
            summary["failed"] += 1
            failures.append((title, str(exc)))
            print(f"FAIL {title}")
            print(f"     {exc}")

    print()
    print(f"Applied: {applied}/{len(planned)}")
    if failures:
        print()
        print("Failures:")
        for title, reason in failures:
            print(f"  - {title}: {reason}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
