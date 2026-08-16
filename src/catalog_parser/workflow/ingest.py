from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from catalog_parser.airtable import (
    AirtableClient,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_STATUS,
    FIELD_TRANSLATOR,
    STATUS_NOT_ASSIGNED,
    STATUS_TODO,
    load_existing_original_video_keys_for_ingest,
    load_existing_original_video_names_for_ingest,
    load_existing_titles_for_ingest,
    load_existing_video_folder_ids_for_ingest,
)
from catalog_parser.auth import get_docs_service, get_drive_service, get_sheets_service
from catalog_parser.parser import (
    DEFAULT_VIDEO_TYPE,
    TYPE_VIDEO,
    filter_by_pkg_tn,
    parse_catalog,
    parse_video_type,
    tn_is_marked,
    type_duration_bounds,
)
from catalog_parser.__main__ import (
    DEFAULT_CREDENTIALS,
    DEFAULT_TOKEN,
    PROJECT_ROOT,
    build_eligible_catalog_records,
)
from catalog_parser.canva import build_canva_client_from_env
from catalog_parser.smartcat import DEFAULT_UI_BASE, configured_target_language
from catalog_parser.smartcat_web import DEFAULT_STORAGE_STATE, SmartcatWebClient
from catalog_parser.workflow.config import load_catalog_id
from catalog_parser.workflow.table_cache import TableCache


def ingest_batch_for_translator(
    airtable: AirtableClient,
    *,
    translator_name: str,
    desired_type: str,
    target_count: int,
    max_video_seconds: int,
    credentials_path: Path = DEFAULT_CREDENTIALS,
    token_path: Path = DEFAULT_TOKEN,
    use_console: bool = False,
    table_cache: TableCache | None = None,
    dry_run: bool = False,
    require_pkg_tn: bool = False,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    return ingest_batch(
        airtable,
        desired_type=desired_type,
        target_count=target_count,
        max_video_seconds=max_video_seconds,
        airtable_fields={FIELD_TRANSLATOR: translator_name, FIELD_STATUS: STATUS_TODO},
        credentials_path=credentials_path,
        token_path=token_path,
        use_console=use_console,
        table_cache=table_cache,
        dry_run=dry_run,
        require_pkg_tn=require_pkg_tn,
        log=log,
    )


def ingest_batch_unassigned(
    airtable: AirtableClient,
    *,
    desired_type: str,
    target_count: int,
    max_video_seconds: int,
    credentials_path: Path = DEFAULT_CREDENTIALS,
    token_path: Path = DEFAULT_TOKEN,
    use_console: bool = False,
    table_cache: TableCache | None = None,
    dry_run: bool = False,
    require_pkg_tn: bool = False,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    return ingest_batch(
        airtable,
        desired_type=desired_type,
        target_count=target_count,
        max_video_seconds=max_video_seconds,
        airtable_fields={FIELD_STATUS: STATUS_NOT_ASSIGNED},
        credentials_path=credentials_path,
        token_path=token_path,
        use_console=use_console,
        table_cache=table_cache,
        dry_run=dry_run,
        require_pkg_tn=require_pkg_tn,
        log=log,
    )


def ingest_batch(
    airtable: AirtableClient,
    *,
    desired_type: str,
    target_count: int,
    max_video_seconds: int,
    airtable_fields: dict[str, Any],
    credentials_path: Path = DEFAULT_CREDENTIALS,
    token_path: Path = DEFAULT_TOKEN,
    use_console: bool = False,
    table_cache: TableCache | None = None,
    dry_run: bool = False,
    require_pkg_tn: bool = False,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    catalog_id = load_catalog_id(PROJECT_ROOT)

    emit = log or print
    video_type = parse_video_type(desired_type or (os.getenv("VIDEO_TYPE") or DEFAULT_VIDEO_TYPE))
    type_min_duration, type_max_duration = type_duration_bounds(video_type)
    min_duration = type_min_duration
    max_duration = type_max_duration
    if video_type == TYPE_VIDEO:
        max_duration = min(max_duration, max_video_seconds)

    service = get_sheets_service(
        credentials_path,
        token_path,
        use_console=use_console,
    )
    candidates = parse_catalog(
        service,
        catalog_id,
        limit=0,
        min_duration=min_duration,
        max_duration=max_duration,
        video_type=video_type,
    )
    if require_pkg_tn:
        before = len(candidates)
        candidates = filter_by_pkg_tn(candidates, require_marked=True)
        emit(
            f"pkgTn filter: {len(candidates)}/{before} {video_type} candidate(s) "
            f"have pkgTn marked (not empty/X)."
        )
    else:
        marked = sum(1 for row in candidates if tn_is_marked(row.get("pkgTn")))
        emit(
            f"Ingest order: {marked} pkgTn-marked {video_type} candidate(s) first, "
            f"then {len(candidates) - marked} unmarked."
        )

    smartcat_language = configured_target_language()
    smartcat_api = os.getenv("SMARTCAT_API", "").strip().lower() in {"1", "true", "yes"}
    storage_state_path = Path(os.getenv("SMARTCAT_STORAGE_STATE", DEFAULT_STORAGE_STATE))

    drive_service = get_drive_service(
        credentials_path,
        token_path,
        use_console=use_console,
    )
    docs_service = get_docs_service(
        credentials_path,
        token_path,
        use_console=use_console,
    )
    canva_client = build_canva_client_from_env(project_root=PROJECT_ROOT)

    web_client = None
    if not smartcat_api:
        web_client = SmartcatWebClient(
            ui_base=os.getenv("SMARTCAT_UI_BASE", DEFAULT_UI_BASE).strip() or DEFAULT_UI_BASE,
            storage_state_path=storage_state_path,
            headless=True,
            language=smartcat_language,
        )

    existing_titles = load_existing_titles_for_ingest(
        airtable,
        table_cache=table_cache,
        project_root=PROJECT_ROOT,
    )
    existing_folder_ids = load_existing_video_folder_ids_for_ingest(
        airtable,
        table_cache=table_cache,
    )
    existing_original_video_names = load_existing_original_video_names_for_ingest(
        airtable,
        table_cache=table_cache,
    )
    existing_original_video_keys = load_existing_original_video_keys_for_ingest(
        airtable,
        table_cache=table_cache,
    )
    staging_dir = PROJECT_ROOT / "output" / "ingest-thumbnails"
    staging_dir.mkdir(parents=True, exist_ok=True)

    enrich_kwargs = {
        "target_count": target_count,
        "existing_titles": existing_titles,
        "existing_folder_ids": existing_folder_ids,
        "existing_original_video_names": existing_original_video_names,
        "existing_original_video_keys": existing_original_video_keys,
        "smartcat_enabled": True,
        "smartcat_api": smartcat_api,
        "smartcat_language": smartcat_language,
        "web_client": web_client,
        "drive_docs_enabled": True,
        "drive_service": drive_service,
        "docs_service": docs_service,
        "canva_client": canva_client,
        "require_mixable_media": True,
        "thumbnail_staging_dir": staging_dir,
        "video_type": video_type,
        "dry_run": dry_run,
    }

    eligible, scanned = build_eligible_catalog_records(candidates, **enrich_kwargs)
    if not eligible and video_type == TYPE_VIDEO:
        candidates = parse_catalog(
            service,
            catalog_id,
            limit=0,
            min_duration=min_duration,
            max_duration=type_max_duration,
            video_type=video_type,
        )
        if require_pkg_tn:
            candidates = filter_by_pkg_tn(candidates, require_marked=True)
        eligible, scanned = build_eligible_catalog_records(candidates, **enrich_kwargs)

    emit(
        f"Ingest scan: {len(eligible)} eligible {video_type}(s) "
        f"after {scanned} candidate(s) (target {target_count})."
    )
    if not eligible:
        return []

    for record in eligible:
        title = record.get("ctTitle") or "Untitled"
        if dry_run:
            emit(f"  would ingest: {title}")
        extras = dict(airtable_fields)
        record["_airtable_fields"] = extras

    if dry_run:
        status_label = airtable_fields.get(FIELD_STATUS, "unknown status")
        emit(f"Dry-run: would create {len(eligible)} Airtable row(s) with status {status_label!r}.")
        # Placeholder ids so callers can report how many rows would be created.
        return [f"dry-run-{index}" for index in range(1, len(eligible) + 1)]

    created_ids = airtable.create_records(eligible)
    review_items: list[Any] = []
    for record, record_id in zip(eligible, created_ids, strict=True):
        title = record.get("ctTitle") or "Untitled"
        emit(f"  ingested: {record_id}\t{title}")
        thumbnail_path = record.get("_originalThumbnailPath")
        if isinstance(thumbnail_path, str) and thumbnail_path.strip():
            path = Path(thumbnail_path)
            if path.is_file():
                airtable.upload_attachment(
                    record_id,
                    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
                    path,
                )
                path.unlink(missing_ok=True)
            continue

        review_path = record.get("_thumbnailReviewPath")
        if not isinstance(review_path, str) or not review_path.strip():
            continue
        path = Path(review_path)
        if not path.is_file():
            continue
        from media_publisher.config import load_settings
        from media_publisher.sources.drive_layout import (
            drive_folder_url,
            resolve_thumbnails_for_approval_id,
        )
        from media_publisher.sources.thumbnail_review import (
            ReviewQueueItem,
            upload_review_thumbnail,
        )

        settings = load_settings(PROJECT_ROOT)
        review_folder_id = resolve_thumbnails_for_approval_id(
            drive_service,
            drive_url=settings.drive_url,
        )
        upload_review_thumbnail(
            drive_service,
            review_folder_id=review_folder_id,
            local_path=path,
            title=str(title),
        )
        review_items.append(
            ReviewQueueItem(
                record_id=record_id,
                title=str(title),
                local_path=path,
                reason="no Canva link; original-platform aspect matches",
            )
        )
        emit(f"  queued for thumbnail review: {title}")
        path.unlink(missing_ok=True)

    if review_items:
        from media_publisher.sources.thumbnail_review import send_review_notification_email

        if send_review_notification_email(
            review_items,
            review_folder_url=drive_folder_url(review_folder_id),
        ):
            emit(f"Thumbnail review email sent ({len(review_items)} video(s)).")
        else:
            emit(
                "WARN: thumbnail review uploads succeeded but notification email "
                "was not sent (check GMAIL_SMTP_* / NOTIFY_EMAIL)."
            )

    if table_cache is not None and created_ids:
        table_cache.register_created_from_catalog(eligible, created_ids)
    emit(f"Created {len(created_ids)} Airtable row(s).")
    return created_ids
