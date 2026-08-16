"""Catch up Videos skipped only because a different Type shared the FIFA title."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from catalog_parser.__main__ import (  # noqa: E402
    DEFAULT_CREDENTIALS,
    DEFAULT_TOKEN,
    PROJECT_ROOT,
    build_eligible_catalog_records,
    load_env_file,
)
from catalog_parser.airtable import (  # noqa: E402
    AirtableClient,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TRANSLATOR,
    FIELD_TYPE,
    STATUS_TODO,
    normalize_title,
    normalize_type_key,
    title_identity_collides,
)
from catalog_parser.auth import (  # noqa: E402
    get_docs_service,
    get_drive_service,
    get_sheets_service,
)
from catalog_parser.canva import build_canva_client_from_env  # noqa: E402
from catalog_parser.eligibility import (  # noqa: E402
    catalog_original_video_key,
    catalog_video_folder_id,
)
from catalog_parser.parser import (  # noqa: E402
    TYPE_VIDEO,
    filter_by_pkg_tn,
    parse_catalog,
    type_duration_bounds,
)
from catalog_parser.runtime_env import materialize_credentials  # noqa: E402
from catalog_parser.smartcat import DEFAULT_UI_BASE, configured_target_language  # noqa: E402
from catalog_parser.smartcat_web import DEFAULT_STORAGE_STATE, SmartcatWebClient  # noqa: E402
from catalog_parser.workflow.config import load_catalog_id  # noqa: E402
from catalog_parser.workflow.table_cache import TableCache  # noqa: E402

TODAY_LOG_TITLES = [
    "Farm vs Supermarket: Ian Somerhalder & Sadhguru Guess",
    "Kalabhairava Sthapana by Sadhguru on Phalguna Purnima",
    "She Had a Perfect Life… And One Day Everything Changed",
    "Take This Simple Step for a Great 2026 | Sadhguru",
    "Sadhguru Speaks About His Mother",
    "This Meditation Opens Up Past Life Memories | Sadhguru",
    "Can Vastu Really Change Your Life? | Sadhguru",
    "Arjuna, Karna, Bhishma or Krishna – Who Was the Greatest of Mahabharat’s Warriors? | Sadhguru",
]


def _titles_by_type(records: list[dict]) -> dict[str, set[str]]:
    by_type: dict[str, set[str]] = {}
    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue
        title = normalize_title(fields.get(FIELD_TITLE))
        if not title:
            continue
        type_key = normalize_type_key(fields.get(FIELD_TYPE))
        by_type.setdefault(type_key, set()).add(title)
    return by_type


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--all-cross-type",
        action="store_true",
        help="All pkgTn Videos with cross-type title collision, not only today's log.",
    )
    parser.add_argument("--translator", default="Genka Petrova")
    args = parser.parse_args()

    load_env_file(PROJECT_ROOT / ".env")
    materialize_credentials(PROJECT_ROOT)
    sa = PROJECT_ROOT / "credentials" / "google-sheets-service-account.json"
    if sa.is_file() and not os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip():
        os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"] = str(sa)

    airtable = AirtableClient(
        token=os.environ["AIRTABLE_TOKEN"].strip(),
        base_id=os.environ["AIRTABLE_BASE_ID"].strip(),
        table_name=os.environ["AIRTABLE_TABLE_NAME"].strip(),
    )
    table_cache = TableCache.load(airtable, project_root=PROJECT_ROOT)
    by_type = _titles_by_type(table_cache.records)
    video_titles = by_type.get("video", set())
    other_titles: set[str] = set()
    for type_key, titles in by_type.items():
        if type_key != "video":
            other_titles |= titles

    typed_keys = table_cache.existing_title_keys()
    folder_ids = table_cache.existing_video_folder_ids()
    original_keys = table_cache.existing_original_video_keys()

    type_min, type_max = type_duration_bounds(TYPE_VIDEO)
    sheets = get_sheets_service(DEFAULT_CREDENTIALS, DEFAULT_TOKEN, use_console=False)
    candidates = parse_catalog(
        sheets,
        load_catalog_id(PROJECT_ROOT),
        limit=0,
        min_duration=type_min,
        max_duration=type_max,
        video_type=TYPE_VIDEO,
    )
    candidates = filter_by_pkg_tn(candidates, require_marked=True)

    log_keys = {normalize_title(t) for t in TODAY_LOG_TITLES if normalize_title(t)}
    catch_up: list[dict] = []
    for row in candidates:
        title = normalize_title(row.get("ctTitle"))
        if not title:
            continue
        if not args.all_cross_type and title not in log_keys:
            continue
        if title_identity_collides(typed_keys, row.get("ctTitle"), "Video"):
            print(f"SKIP same-type title: {row.get('ctTitle')}")
            continue
        folder_id = catalog_video_folder_id(row)
        if folder_id and folder_id in folder_ids:
            print(f"SKIP folder: {row.get('ctTitle')}")
            continue
        ov_key = catalog_original_video_key(row)
        if ov_key and ov_key in original_keys:
            print(f"SKIP original video URL: {row.get('ctTitle')}")
            continue
        if title in video_titles:
            print(f"SKIP already Video: {row.get('ctTitle')}")
            continue
        if title not in other_titles and title not in log_keys:
            continue
        other = sorted(t for t, titles in by_type.items() if title in titles and t != "video")
        print(f"CATCH-UP: {row.get('ctTitle')}  (exists as: {', '.join(other) or '?'})")
        catch_up.append(row)

    print(f"\nTo ingest: {len(catch_up)}")
    if not catch_up:
        return 0
    if args.dry_run:
        print("Dry-run only.")
        return 0

    drive = get_drive_service(DEFAULT_CREDENTIALS, DEFAULT_TOKEN, use_console=False)
    docs = get_docs_service(DEFAULT_CREDENTIALS, DEFAULT_TOKEN, use_console=False)
    canva = build_canva_client_from_env(project_root=PROJECT_ROOT)
    smartcat_language = configured_target_language()
    web_client = SmartcatWebClient(
        ui_base=os.getenv("SMARTCAT_UI_BASE", DEFAULT_UI_BASE).strip() or DEFAULT_UI_BASE,
        storage_state_path=Path(os.getenv("SMARTCAT_STORAGE_STATE", DEFAULT_STORAGE_STATE)),
        headless=True,
        language=smartcat_language,
    )
    staging = PROJECT_ROOT / "output" / "ingest-thumbnails"
    staging.mkdir(parents=True, exist_ok=True)

    eligible, scanned = build_eligible_catalog_records(
        catch_up,
        target_count=len(catch_up),
        existing_titles=set(typed_keys),
        existing_folder_ids=set(folder_ids),
        existing_original_video_names=table_cache.existing_original_video_names(),
        existing_original_video_keys=set(original_keys),
        smartcat_enabled=True,
        smartcat_api=False,
        smartcat_language=smartcat_language,
        web_client=web_client,
        drive_docs_enabled=True,
        drive_service=drive,
        docs_service=docs,
        canva_client=canva,
        require_mixable_media=True,
        thumbnail_staging_dir=staging,
        video_type=TYPE_VIDEO,
    )
    print(f"Eligible after enrich: {len(eligible)} / scanned {scanned}")
    if not eligible:
        return 1

    for record in eligible:
        extras = {
            FIELD_TRANSLATOR: args.translator,
            FIELD_STATUS: STATUS_TODO,
        }
        print(f"  {record.get('ctTitle')}: {STATUS_TODO}")
        record["_airtable_fields"] = extras

    created_ids = airtable.create_records(eligible)
    for record, record_id in zip(eligible, created_ids, strict=True):
        print(f"  ingested: {record_id}\t{record.get('ctTitle')}")
        thumb = record.get("_originalThumbnailPath")
        if isinstance(thumb, str) and Path(thumb).is_file():
            airtable.upload_attachment(
                record_id,
                FIELD_ORIGINAL_VIDEO_THUMBNAIL,
                Path(thumb),
            )
            Path(thumb).unlink(missing_ok=True)
    print(f"Created {len(created_ids)} Airtable row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
