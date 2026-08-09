from __future__ import annotations

from typing import Any

from catalog_parser.airtable import (
    normalize_original_video_key,
    normalize_original_video_name_key,
    normalize_title,
    resolve_record_type_key,
    title_identity_collides,
    title_identity_keys,
)
from catalog_parser.drive_docs import extract_drive_folder_id
from catalog_parser.drive_mix import check_mixable_media, record_has_mixable_media
from googleapiclient.discovery import Resource

def needs_bulgarian_translation(record: dict[str, Any]) -> bool:
    pkg_bg_srt_link = record.get("pkgBgSrtLk")
    return isinstance(pkg_bg_srt_link, str) and bool(pkg_bg_srt_link.strip())


def catalog_video_folder_id(record: dict[str, Any]) -> str | None:
    """Drive folder id from catalog ``pkgLink`` (Airtable Video Folder)."""
    link = record.get("pkgLink")
    if not isinstance(link, str) or not link.strip():
        return None
    return extract_drive_folder_id(link)


def catalog_yt_title_key(record: dict[str, Any]) -> str | None:
    """Normalized ytTitle key for Original Video Name dedup."""
    return normalize_original_video_name_key(record.get("ytTitle"))


def catalog_original_video_key(record: dict[str, Any]) -> str | None:
    """Platform id key from catalog ``ctLink`` (Airtable Original Video)."""
    return normalize_original_video_key(record.get("ctLink"))


def is_not_in_airtable(
    record: dict[str, Any],
    existing_titles: set[str],
    *,
    video_type: str | None = None,
) -> bool:
    title = normalize_title(record.get("ctTitle"))
    if not title:
        return False
    type_key = resolve_record_type_key(record, video_type=video_type)
    return not title_identity_collides(
        existing_titles,
        record.get("ctTitle"),
        type_key,
    )


def is_not_duplicate_video_folder(
    record: dict[str, Any],
    existing_folder_ids: set[str],
) -> bool:
    """True when the Drive package folder is new or missing/unparseable."""
    folder_id = catalog_video_folder_id(record)
    if not folder_id:
        return True
    return folder_id not in existing_folder_ids


def is_not_duplicate_yt_title(
    record: dict[str, Any],
    existing_original_video_names: set[str],
) -> bool:
    """True when ytTitle is new or missing (same posture as unparseable folder)."""
    key = catalog_yt_title_key(record)
    if not key:
        return True
    return key not in existing_original_video_names


def is_not_duplicate_original_video(
    record: dict[str, Any],
    existing_original_video_keys: set[str],
) -> bool:
    """True when ctLink platform id is new or unparseable."""
    key = catalog_original_video_key(record)
    if not key:
        return True
    return key not in existing_original_video_keys


def airtable_identity_collision_reasons(
    record: dict[str, Any],
    existing_titles: set[str],
    *,
    existing_folder_ids: set[str] | None = None,
    existing_original_video_names: set[str] | None = None,
    existing_original_video_keys: set[str] | None = None,
    video_type: str | None = None,
) -> list[str]:
    """Cheap Airtable identity collisions (sheet fields only; no Smartcat/Drive)."""
    reasons: list[str] = []
    if not is_not_in_airtable(record, existing_titles, video_type=video_type):
        reasons.append("Already in Airtable (duplicate title for this Type)")
    if existing_folder_ids is not None and not is_not_duplicate_video_folder(
        record,
        existing_folder_ids,
    ):
        reasons.append("Already in Airtable (duplicate Video Folder)")
    if (
        existing_original_video_names is not None
        and not is_not_duplicate_yt_title(record, existing_original_video_names)
    ):
        reasons.append("Already in Airtable (duplicate Original Video Name)")
    if (
        existing_original_video_keys is not None
        and not is_not_duplicate_original_video(record, existing_original_video_keys)
    ):
        reasons.append("Already in Airtable (duplicate Original Video)")
    return reasons


def is_catalog_eligible(
    record: dict[str, Any],
    existing_titles: set[str],
    *,
    existing_folder_ids: set[str] | None = None,
    existing_original_video_names: set[str] | None = None,
    existing_original_video_keys: set[str] | None = None,
    drive_service: Resource | None = None,
    require_smartcat: bool = True,
    require_mixable_media: bool = True,
    video_type: str | None = None,
) -> bool:
    if require_smartcat and not needs_bulgarian_translation(record):
        return False
    if airtable_identity_collision_reasons(
        record,
        existing_titles,
        existing_folder_ids=existing_folder_ids,
        existing_original_video_names=existing_original_video_names,
        existing_original_video_keys=existing_original_video_keys,
        video_type=video_type,
    ):
        return False
    if require_mixable_media:
        if drive_service is None:
            return False
        if not record_has_mixable_media(
            drive_service,
            record,
            video_type=video_type,
        ):
            return False
    return True


def explain_catalog_eligibility(
    record: dict[str, Any],
    existing_titles: set[str],
    *,
    existing_folder_ids: set[str] | None = None,
    existing_original_video_names: set[str] | None = None,
    existing_original_video_keys: set[str] | None = None,
    drive_service: Resource | None = None,
    require_smartcat: bool = True,
    require_mixable_media: bool = True,
    video_type: str | None = None,
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

    reasons.extend(
        airtable_identity_collision_reasons(
            record,
            existing_titles,
            existing_folder_ids=existing_folder_ids,
            existing_original_video_names=existing_original_video_names,
            existing_original_video_keys=existing_original_video_keys,
            video_type=video_type,
        )
    )

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
                    check = check_mixable_media(
                        drive_service,
                        folder_id,
                        video_type=video_type,
                    )
                    if not check.ok:
                        detail = check.error or "unknown error"
                        reasons.append(f"Drive mix: {detail}")

    return reasons


def register_title_identity(
    existing_titles: set[str],
    record: dict[str, Any],
    *,
    video_type: str | None = None,
) -> None:
    type_key = resolve_record_type_key(record, video_type=video_type)
    existing_titles.update(title_identity_keys(record.get("ctTitle"), type_key))
