"""Retry failed pkgTn thumbnail cache entries."""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from catalog_parser.canva_selection import select_canva_url
from catalog_parser.drive_video_size import video_size_from_pkg_folder
from scripts.cache_pkgtn_thumbnails import (
    DEFAULT_OUTPUT_DIR,
    FIELD_ORIGINAL_VIDEO_NAME,
    build_filter_formula,
    destination_for_title,
    extract_canva_links,
    flatten_drive_file,
    index_catalog,
    is_thumbnail_file,
    local_source_path,
    match_catalog_row,
    parse_folder_id,
    pick_root_thumbnail,
    read_word_document,
    save_jpg,
    status_bucket,
    tn_is_marked,
    document_sort_key,
    fetch_catalog_records,
)
from media_publisher.__main__ import canva_client_from_settings, canva_settings_complete
from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_VIDEO_FOLDER,
    TYPE_QUOTE,
    AirtableClient,
)
from scripts.cache_pkgtn_thumbnails import canva_design_id
from media_publisher.sources.google_drive import GoogleDriveClient

WORD_DOC_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"

FAILURES = [
    "Sadhguru Talks About Inter Caste Marriage In The Context Of Indias Social Fabric",
    "Sadhgurus Message On Pahalgam Terror Attack",
    "What Sachins Cricket Game Can Teach You",
    "When Does Someone Become Yours",
    "Why We Worship Jesus | Sadhguru",
    "9 Hindu Avatars & Darwin's Theory of Evolution",
    "Fruits and Cooked Meals - The Right Order?",
]


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
    canva_client = canva_client_from_settings(settings) if canva_settings_complete(settings) else None
    catalog = fetch_catalog_records()
    by_url, by_title = index_catalog(catalog)
    output_dir = DEFAULT_OUTPUT_DIR
    manifest_path = output_dir / "pkgtn-thumbnails-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else []
    manifest_by_title = {entry["title"]: entry for entry in manifest}

    import tempfile
    from PIL import Image

    ok = 0
    for record in airtable.list_records(filter_formula=build_filter_formula()):
        fields = record.fields
        title = str(
            fields.get(FIELD_ORIGINAL_VIDEO_NAME) or fields.get(FIELD_TITLE) or ""
        ).strip()
        if title not in FAILURES:
            continue
        folder_id = parse_folder_id(fields.get(FIELD_VIDEO_FOLDER))
        destination = destination_for_title(output_dir, title)
        try:
            with tempfile.TemporaryDirectory(prefix="tn-retry-") as tmp:
                tmp_path = Path(tmp)
                children = drive.list_children(folder_id)
                root_file = pick_root_thumbnail(children)
                if root_file is not None:
                    local_source = local_source_path(
                        tmp_path, root_file.name, root_file.mime_type
                    )
                    drive.download_file(root_file.id, local_source)
                    image = flatten_drive_file(
                        local_source, mime_type=root_file.mime_type
                    )
                    save_jpg(image, destination)
                    source_type = "drive"
                else:
                    docs = sorted(
                        [
                            child
                            for child in children
                            if child.mime_type in (WORD_DOC_MIME, GOOGLE_DOC_MIME)
                            and child.name.upper().startswith("TEXT_")
                        ],
                        key=document_sort_key,
                    )
                    document = read_word_document(drive, docs[0])
                    canva_any, _canva_below_tn = extract_canva_links(document)
                    canva_url = select_canva_url(
                        canva_any,
                        target_size=video_size_from_pkg_folder(drive.drive_service, folder_id),
                        original_video_url=str(fields.get(FIELD_ORIGINAL_VIDEO) or ""),
                    )
                    temp_export = tmp_path / "preview.bin"
                    if canva_client is None:
                        raise RuntimeError("Canva client is not configured")
                    canva_client.download_design_image(
                        canva_design_id(canva_url),
                        temp_export.with_suffix(".jpg"),
                        export_format="jpg",
                    )
                    temp_export = temp_export.with_suffix(".jpg")
                    source_type = "canva-export"
                    with Image.open(temp_export) as image:
                        save_jpg(image, destination)
                manifest_by_title[title] = {
                    "title": title,
                    "status": status_bucket(fields.get(FIELD_STATUS)) or "",
                    "source_type": source_type,
                    "source_detail": title,
                    "airtable_file": str(destination),
                }
                ok += 1
                print(f"OK  {title} -> {destination.name}")
        except Exception as exc:
            print(f"FAIL {title}: {exc}")

    manifest_path.write_text(
        json.dumps(list(manifest_by_title.values()), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nRetried OK: {ok}/{len(FAILURES)}")
    return 0 if ok == len(FAILURES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
