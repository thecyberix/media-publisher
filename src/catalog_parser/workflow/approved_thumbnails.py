"""Upload approved Drive review thumbnails into Airtable during catalog workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

LogFn = Callable[..., None]


@dataclass(frozen=True)
class ApprovedThumbnailRunResult:
    processed: int
    skipped: bool = False

    @property
    def success(self) -> bool:
        return True


def process_approved_review_thumbnails_in_workflow(
    *,
    project_root: Path,
    records: list[dict[str, Any]],
    dry_run: bool,
    log: LogFn = print,
) -> ApprovedThumbnailRunResult:
    from media_publisher.config import load_settings
    from media_publisher.sources.airtable import AirtableClient
    from media_publisher.sources.google_drive import GoogleDriveClient
    from media_publisher.sources.thumbnail_review import process_approved_review_thumbnails

    settings = load_settings(project_root)
    service_account_path = project_root / settings.google_sheets_service_account
    if not service_account_path.is_file():
        log(
            "Approved thumbnails: skipped "
            "(google-sheets-service-account.json not available)"
        )
        return ApprovedThumbnailRunResult(processed=0, skipped=True)

    if (
        not settings.airtable_token
        or not settings.airtable_base_id
        or not settings.airtable_table_name
    ):
        log("Approved thumbnails: skipped (Airtable env vars not configured)")
        return ApprovedThumbnailRunResult(processed=0, skipped=True)

    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    drive = GoogleDriveClient.from_service_account(service_account_path)
    adapted_records = [
        SimpleNamespace(id=record["id"], fields=record.get("fields", {}))
        for record in records
    ]

    results = process_approved_review_thumbnails(
        airtable,
        drive,
        adapted_records,
        review_folder_id=settings.thumbnail_review_drive_folder_id,
        approved_subfolder=settings.thumbnail_review_approved_subfolder,
        apply=not dry_run,
        project_root=project_root,
    )

    if not results:
        log("Approved thumbnails: none waiting in Drive Approved folder")
        return ApprovedThumbnailRunResult(processed=0)

    label = "planned" if dry_run else "processed"
    log(f"Approved thumbnails: {label} {len(results)} file(s)")
    for item in sorted(results, key=lambda row: row.title.casefold()):
        caption = item.caption_action
        if item.caption_detail:
            caption = f"{item.caption_action} ({item.caption_detail})"
        log(f"  - {item.title}: {item.action} ({item.drive_file}); caption={caption}")
    return ApprovedThumbnailRunResult(processed=len(results))
