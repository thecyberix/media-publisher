"""Pre-generate FB/YT quote JPEGs and sync them to the generated-quotes Drive folder."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from media_publisher.quotes_render_pipeline import (
    QuotesRenderPipelineError,
    render_monthly_quotes,
)
from media_publisher.sources.drive_layout import (
    drive_folder_url as quotes_drive_folder_link,
    resolve_quotes_folder_id,
)
from media_publisher.sources.google_drive import (
    GoogleDriveClient,
    GoogleDriveError,
    QuoteBackgroundImage,
    UploadAction,
    format_month_folder_name,
    local_file_md5,
)
from media_publisher.sources.google_sheets import GoogleSheetsClient, GoogleSheetsError
from media_publisher.sources.quotes_config import QuotesSourcesConfig
from media_publisher.sources.quotes_sheet import DailyQuoteText, QuotesSheetError, load_monthly_quote_texts

GENERATED_MONTH_FOLDER_PATTERN = "{month:02d} {month_abbr} {year}"
SYNC_STATE_RELATIVE_PATH = Path("downloads/quotes/generated-sync-state.json")
DEFAULT_VARIANT = "fbyt"


def _parse_email_list(raw: str) -> list[str]:
    """Split a comma/semicolon-separated email list, preserving order and uniqueness."""
    recipients: list[str] = []
    for part in raw.replace(";", ",").split(","):
        text = part.strip()
        if text and text not in recipients:
            recipients.append(text)
    return recipients


@dataclass(frozen=True)
class GeneratedQuoteChange:
    action: UploadAction
    year: int
    month: int
    day: int
    drive_name: str
    caption: str
    fingerprint: str
    source: str = "ready"


@dataclass(frozen=True)
class GeneratedQuotesSyncResult:
    changes: list[GeneratedQuoteChange]
    warnings: list[str]

    @property
    def added_count(self) -> int:
        return sum(1 for item in self.changes if item.action == "added")

    @property
    def updated_count(self) -> int:
        return sum(1 for item in self.changes if item.action == "updated")


def current_and_next_months(reference: date) -> list[tuple[int, int]]:
    year = reference.year
    month = reference.month
    if month == 12:
        return [(year, month), (year + 1, 1)]
    return [(year, month), (year, month + 1)]


def pair_fingerprint(*, background: QuoteBackgroundImage, text: str) -> str:
    bg_token = background.md5_checksum or background.modified_time or background.file_id
    payload = f"{background.file_id}|{bg_token}|{text.strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def substitute_quote_fingerprint(*, image_path: Path, caption: str) -> str:
    payload = f"substitute|{local_file_md5(image_path)}|{caption.strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upload_published_substitute_quote(
    *,
    drive_client: GoogleDriveClient,
    project_root: Path,
    image_path: Path,
    year: int,
    month: int,
    day: int,
    caption: str,
    source: str = "edited",
    print_line: Callable[[str], None] | None = None,
) -> tuple[GeneratedQuoteChange | None, list[str]]:
    """Upload a scheduled/published quote that used Edited or Translation."""
    log = print_line or (lambda _message: None)
    warnings: list[str] = []
    if not image_path.is_file():
        warnings.append(
            f"{year:04d}-{month:02d}-{day:02d}: substitute Drive upload skipped "
            f"(missing {image_path})"
        )
        return None, warnings

    state_path = project_root / SYNC_STATE_RELATIVE_PATH
    state = load_sync_state(state_path)
    fingerprint = substitute_quote_fingerprint(image_path=image_path, caption=caption)
    drive_name = f"{year:04d}-{month:02d}-{day:02d}.jpg"
    output_month_name = format_month_folder_name(
        GENERATED_MONTH_FOLDER_PATTERN,
        year=year,
        month=month,
    )
    try:
        output_month = drive_client.ensure_folder(
            resolve_quotes_folder_id(drive_client),
            output_month_name,
        )
        upload = drive_client.upload_or_update_file(
            output_month.id,
            image_path,
            name=drive_name,
            mime_type="image/jpeg",
        )
    except GoogleDriveError as exc:
        warnings.append(f"{drive_name}: substitute Drive upload failed ({exc})")
        return None, warnings

    state[state_key(year=year, month=month, day=day)] = {
        "fingerprint": fingerprint,
        "drive_file_id": upload.file.id,
    }
    save_sync_state(state_path, state)
    if upload.action == "unchanged":
        log(f"Unchanged Drive quote: {output_month_name}/{drive_name} (substitute)")
        return None, warnings
    log(
        f"{upload.action.capitalize()} Drive quote: "
        f"{output_month_name}/{drive_name} (substitute)"
    )
    return (
        GeneratedQuoteChange(
            action=upload.action,
            year=year,
            month=month,
            day=day,
            drive_name=drive_name,
            caption=caption,
            fingerprint=fingerprint,
            source=source,
        ),
        warnings,
    )


def state_key(*, year: int, month: int, day: int, variant: str = DEFAULT_VARIANT) -> str:
    return f"{year:04d}-{month:02d}-{day:02d}:{variant}"


def load_sync_state(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        fingerprint = value.get("fingerprint")
        if isinstance(fingerprint, str) and fingerprint:
            entry: dict[str, str] = {"fingerprint": fingerprint}
            drive_file_id = value.get("drive_file_id")
            if isinstance(drive_file_id, str) and drive_file_id:
                entry["drive_file_id"] = drive_file_id
            result[key] = entry
    return result


def save_sync_state(path: Path, state: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def generated_quotes_notify_recipients() -> list[str]:
    """Recipients from GENERATED_QUOTES_NOTIFY_EMAIL (comma-separated list)."""
    return _parse_email_list(os.getenv("GENERATED_QUOTES_NOTIFY_EMAIL", ""))


def _email_change_line(item: GeneratedQuoteChange) -> str:
    excerpt = item.caption.replace("\n", " ").strip()
    if len(excerpt) > 120:
        excerpt = excerpt[:117] + "..."
    line = f"  - {item.drive_name}: {excerpt}"
    if item.source != "ready":
        line += f" ({item.source} substitute)"
    return line


def format_generated_quotes_email(
    changes: list[GeneratedQuoteChange],
    *,
    drive_folder_url: str = "",
) -> tuple[str, str]:
    added = [item for item in changes if item.action == "added"]
    updated = [item for item in changes if item.action == "updated"]
    subject = (
        f"Generated quotes updated ({len(added)} added, {len(updated)} updated)"
    )
    folder_line = (
        drive_folder_url
        or os.getenv("DRIVE_URL", "").strip()
        or "https://drive.google.com/drive/folders/"
    )
    lines = [
        "Generated quote images were added or updated in Drive.",
        "",
        f"Folder: {folder_line}",
        f"Added: {len(added)}",
        f"Updated: {len(updated)}",
        "",
    ]
    if added:
        lines.append("Added:")
        for item in added:
            lines.append(_email_change_line(item))
        lines.append("")
    if updated:
        lines.append("Updated:")
        for item in updated:
            lines.append(_email_change_line(item))
        lines.append("")
    return subject, "\n".join(lines).rstrip() + "\n"


def send_generated_quotes_notification_email(
    changes: list[GeneratedQuoteChange],
    *,
    to_addresses: list[str] | None = None,
    drive_folder_url: str = "",
) -> bool:
    """Email quote Drive add/update summary to GENERATED_QUOTES_NOTIFY_EMAIL recipients."""
    if not changes:
        return False

    recipients = list(to_addresses) if to_addresses is not None else generated_quotes_notify_recipients()
    if not recipients:
        return False

    scripts_dir = Path(__file__).resolve().parents[2] / "scripts" / "catalog"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from send_notification_email import send_email

    smtp_user = os.getenv("GMAIL_SMTP_USER", "").strip()
    smtp_password = os.getenv("GMAIL_SMTP_APP_PASSWORD", "").strip()
    if not smtp_user or not smtp_password:
        return False

    subject, body = format_generated_quotes_email(
        changes, drive_folder_url=drive_folder_url
    )
    for to_address in recipients:
        send_email(
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            to_address=to_address,
            subject=subject,
            body=body,
        )
    return True


def notify_generated_quote_changes(
    changes: list[GeneratedQuoteChange],
    *,
    drive_folder_url: str = "",
    print_line: Callable[[str], None] | None = None,
) -> list[str]:
    """Email Drive add/update summary. Returns warnings; does not raise."""
    if not changes:
        return []
    log = print_line or (lambda _message: None)
    warnings: list[str] = []
    recipients = generated_quotes_notify_recipients()
    try:
        sent = send_generated_quotes_notification_email(
            changes, drive_folder_url=drive_folder_url
        )
    except Exception as exc:  # noqa: BLE001 — email must not fail the sync
        return [f"Failed to send generated-quotes email: {exc}"]
    if sent:
        log(f"Sent generated-quotes email ({len(recipients)} recipient(s))")
        return []
    reason = (
        "missing GENERATED_QUOTES_NOTIFY_EMAIL"
        if not recipients
        else "missing Gmail SMTP settings"
    )
    warnings.append(f"Generated-quotes email skipped ({reason})")
    return warnings


def _days_needing_render(
    *,
    quotes: list[DailyQuoteText],
    backgrounds: dict[int, QuoteBackgroundImage],
    state: dict[str, dict[str, str]],
    year: int,
    month: int,
    render_dir: Path,
) -> tuple[set[int], set[int], dict[int, str]]:
    """Return (days_to_consider, days_to_overwrite, fingerprints)."""
    consider: set[int] = set()
    overwrite: set[int] = set()
    fingerprints: dict[int, str] = {}
    for quote in quotes:
        background = backgrounds.get(quote.day)
        if background is None:
            continue
        fingerprint = pair_fingerprint(background=background, text=quote.text_bg)
        fingerprints[quote.day] = fingerprint
        consider.add(quote.day)
        key = state_key(year=year, month=month, day=quote.day)
        previous = state.get(key, {}).get("fingerprint")
        local_path = render_dir / f"{year:04d}-{month:02d}-{quote.day:02d}.jpg"
        if previous != fingerprint or not local_path.is_file():
            overwrite.add(quote.day)
    return consider, overwrite, fingerprints


def sync_generated_quotes_for_month(
    *,
    config: QuotesSourcesConfig,
    sheets_client: GoogleSheetsClient,
    drive_client: GoogleDriveClient,
    year: int,
    month: int,
    state: dict[str, dict[str, str]],
    project_root: Path,
    font_path: Path | None = None,
    print_line: Callable[[str], None] | None = None,
    quotes_root_id: str | None = None,
) -> tuple[list[GeneratedQuoteChange], list[str]]:
    log = print_line or (lambda _message: None)
    warnings: list[str] = []
    changes: list[GeneratedQuoteChange] = []

    try:
        from media_publisher.quotes_text_sync import resolve_bulgarian_spreadsheet_id

        quotes = load_monthly_quote_texts(
            sheets_client,
            config,
            year=year,
            month=month,
            require_ready=True,
            spreadsheet_id=resolve_bulgarian_spreadsheet_id(
                drive=drive_client, config=config, year=year
            ),
        )
    except (QuotesSheetError, GoogleSheetsError) as exc:
        warnings.append(f"{year:04d}-{month:02d}: skipped sheet ({exc})")
        return changes, warnings

    drive_config = config.backgrounds_drive
    try:
        month_folder = drive_client.resolve_month_background_folder(
            root_folder_id=str(drive_config["root_folder_id"]),
            year=year,
            month=month,
            year_folder_pattern=str(drive_config["year_folder_pattern"]),
            month_folder_pattern=str(drive_config["month_folder_pattern"]),
        )
    except GoogleDriveError as exc:
        warnings.append(f"{year:04d}-{month:02d}: skipped backgrounds ({exc})")
        return changes, warnings

    variant_drive = drive_config.get("variants", {}).get(DEFAULT_VARIANT, {})
    subdir = variant_drive.get("subdir") if isinstance(variant_drive, dict) else None
    backgrounds_list = drive_client.list_quote_backgrounds(
        month_folder_id=month_folder.id,
        variant=DEFAULT_VARIANT,
        subdir=subdir if isinstance(subdir, str) and subdir.strip() else None,
        month=month,
    )
    backgrounds = {item.day: item for item in backgrounds_list}

    paired_quotes = [quote for quote in quotes if quote.day in backgrounds]
    if not paired_quotes:
        warnings.append(
            f"{year:04d}-{month:02d}: no days with both Ready text and background image"
        )
        return changes, warnings

    days_to_consider, days_to_overwrite, fingerprints = _days_needing_render(
        quotes=paired_quotes,
        backgrounds=backgrounds,
        state=state,
        year=year,
        month=month,
        render_dir=config.variant_render_dir(DEFAULT_VARIANT),
    )
    if not days_to_consider:
        return changes, warnings

    # Render one day at a time so we only overwrite when the pair fingerprint changed.
    rendered_by_day: dict[int, Path] = {}
    captions_by_day = {quote.day: quote.text_bg for quote in paired_quotes}
    for day in sorted(days_to_consider):
        try:
            rendered = render_monthly_quotes(
                config=config,
                sheets_client=sheets_client,
                drive_client=drive_client,
                year=year,
                month=month,
                variants=(DEFAULT_VARIANT,),
                font_path=font_path,
                overwrite=day in days_to_overwrite,
                day=day,
            )
        except QuotesRenderPipelineError as exc:
            warnings.append(f"{year:04d}-{month:02d}-{day:02d}: render failed ({exc})")
            continue
        if not rendered:
            warnings.append(f"{year:04d}-{month:02d}-{day:02d}: render produced no image")
            continue
        rendered_by_day[day] = rendered[0].image_path

    output_month_name = format_month_folder_name(
        GENERATED_MONTH_FOLDER_PATTERN,
        year=year,
        month=month,
    )
    output_month = drive_client.ensure_folder(
        quotes_root_id or resolve_quotes_folder_id(drive_client),
        output_month_name,
    )

    for day, image_path in sorted(rendered_by_day.items()):
        drive_name = f"{year:04d}-{month:02d}-{day:02d}.jpg"
        fingerprint = fingerprints[day]
        try:
            upload = drive_client.upload_or_update_file(
                output_month.id,
                image_path,
                name=drive_name,
                mime_type="image/jpeg",
            )
        except GoogleDriveError as exc:
            warnings.append(f"{drive_name}: upload failed ({exc})")
            continue

        state[state_key(year=year, month=month, day=day)] = {
            "fingerprint": fingerprint,
            "drive_file_id": upload.file.id,
        }
        if upload.action == "unchanged":
            log(f"Unchanged Drive quote: {output_month_name}/{drive_name}")
            continue

        change = GeneratedQuoteChange(
            action=upload.action,
            year=year,
            month=month,
            day=day,
            drive_name=drive_name,
            caption=captions_by_day.get(day, ""),
            fingerprint=fingerprint,
        )
        changes.append(change)
        log(f"{upload.action.capitalize()} Drive quote: {output_month_name}/{drive_name}")

    # Keep state path relative awareness for callers.
    _ = project_root
    return changes, warnings


def sync_generated_quotes_for_months(
    *,
    config: QuotesSourcesConfig,
    sheets_client: GoogleSheetsClient,
    drive_client: GoogleDriveClient,
    project_root: Path,
    reference_date: date,
    font_path: Path | None = None,
    print_line: Callable[[str], None] | None = None,
    send_email: bool = True,
) -> GeneratedQuotesSyncResult:
    log = print_line or (lambda _message: None)
    state_path = project_root / SYNC_STATE_RELATIVE_PATH
    state = load_sync_state(state_path)
    all_changes: list[GeneratedQuoteChange] = []
    all_warnings: list[str] = []

    quotes_root_id = resolve_quotes_folder_id(drive_client)
    quotes_folder_link = quotes_drive_folder_link(quotes_root_id)

    for year, month in current_and_next_months(reference_date):
        log(f"Syncing generated quotes for {year:04d}-{month:02d} ...")
        changes, warnings = sync_generated_quotes_for_month(
            config=config,
            sheets_client=sheets_client,
            drive_client=drive_client,
            year=year,
            month=month,
            state=state,
            project_root=project_root,
            font_path=font_path,
            print_line=print_line,
            quotes_root_id=quotes_root_id,
        )
        all_changes.extend(changes)
        all_warnings.extend(warnings)

    save_sync_state(state_path, state)

    if send_email:
        all_warnings.extend(
            notify_generated_quote_changes(
                all_changes,
                drive_folder_url=quotes_folder_link,
                print_line=log,
            )
        )

    return GeneratedQuotesSyncResult(changes=all_changes, warnings=all_warnings)
