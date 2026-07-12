from __future__ import annotations

from typing import Any

from catalog_parser.airtable import normalize_title
from catalog_parser.drive_docs import extract_drive_folder_id
from catalog_parser.drive_mix import check_mixable_media, record_has_mixable_media
from googleapiclient.discovery import Resource


def needs_bulgarian_translation(record: dict[str, Any]) -> bool:
    pkg_bg_srt_link = record.get("pkgBgSrtLk")
    return isinstance(pkg_bg_srt_link, str) and bool(pkg_bg_srt_link.strip())


def is_not_in_airtable(
    record: dict[str, Any],
    existing_titles: set[str],
) -> bool:
    title = normalize_title(record.get("ctTitle"))
    if not title:
        return False
    return title not in existing_titles


def is_catalog_eligible(
    record: dict[str, Any],
    existing_titles: set[str],
    *,
    drive_service: Resource | None = None,
    require_smartcat: bool = True,
    require_mixable_media: bool = True,
) -> bool:
    if require_smartcat and not needs_bulgarian_translation(record):
        return False
    if not is_not_in_airtable(record, existing_titles):
        return False
    if require_mixable_media:
        if drive_service is None:
            return False
        if not record_has_mixable_media(drive_service, record):
            return False
    return True


def explain_catalog_eligibility(
    record: dict[str, Any],
    existing_titles: set[str],
    *,
    drive_service: Resource | None = None,
    require_smartcat: bool = True,
    require_mixable_media: bool = True,
) -> list[str]:
    reasons: list[str] = []

    if require_smartcat and not needs_bulgarian_translation(record):
        skip_reason = record.get("pkgBgSrtLkSkipReason")
        error = record.get("pkgBgSrtLkError")
        if isinstance(skip_reason, str) and skip_reason.strip():
            reasons.append(f"Smartcat: {skip_reason.strip()}")
        elif isinstance(error, str) and error.strip():
            reasons.append(f"Smartcat error: {error.strip()}")
        elif not record.get("pkgSmLk"):
            reasons.append("Smartcat: missing pkgSmLk in catalog sheet")
        else:
            reasons.append("Smartcat: no Bulgarian SRT editor link resolved")

    if not is_not_in_airtable(record, existing_titles):
        reasons.append("Already in Airtable (duplicate title)")

    if require_mixable_media:
        if drive_service is None:
            reasons.append("Drive mix: Drive service unavailable")
        else:
            folder_link = record.get("pkgLink")
            if not isinstance(folder_link, str) or not folder_link.strip():
                reasons.append("Drive mix: missing pkgLink")
            else:
                folder_id = extract_drive_folder_id(folder_link)
                if folder_id is None:
                    reasons.append("Drive mix: could not parse pkgLink folder id")
                else:
                    check = check_mixable_media(drive_service, folder_id)
                    if not check.ok:
                        detail = check.error or "unknown error"
                        reasons.append(f"Drive mix: {detail}")

    return reasons
